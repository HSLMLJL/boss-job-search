"""Fail-closed account-risk gate for the local radar.

The radar databases remain evidence only: this module opens them read-only and
stores human acknowledgements in a separate append-only safety ledger.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MAX_CANDIDATE_RUNS_PER_DATABASE = 500
_RISK_CODES = {32, 36, 37}
_RISK_STOP_CODE = "ACCOUNT_RISK"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_CODE_32_PATTERN = re.compile(r"\bcode\s*[:=]?\s*32\b", re.IGNORECASE)
_SENSITIVE_MARKERS = ("http://", "https://", "token", "cookie", "seed", "session", "challenge")


class RadarSafetyError(RuntimeError):
	"""The safety gate cannot reliably determine whether a risk remains."""


def list_unresolved_radar_incidents(data_dir: Path) -> list[dict[str, object]]:
	"""Return every unresolved platform-risk incident in stable time order.

	Only ``radar_runs`` metadata is read from source databases. A malformed or
	unreadable candidate record fails closed instead of silently allowing another
	platform request.
	"""
	radar_dir = data_dir / "radar"
	if not radar_dir.exists():
		return []
	incidents: list[dict[str, object]] = []
	for database_path in sorted(radar_dir.glob("*_radar.db"), key=lambda path: path.name.casefold()):
		incidents.extend(_incidents_from_database(database_path))
	resolved_ids = _resolved_incident_ids(radar_dir)
	unresolved = [item for item in incidents if item["incident_id"] not in resolved_ids]
	return sorted(unresolved, key=lambda item: (str(item["occurred_at"]), str(item["incident_id"])))


def resolve_radar_incident(data_dir: Path, incident_id: str) -> dict[str, object]:
	"""Append acknowledgements for the current unresolved incident set.

	The caller must name the newest unresolved incident. That explicit confirmation
	resolves only the exact incidents observed in this call; future incidents never
	inherit the acknowledgement.
	"""
	incidents = list_unresolved_radar_incidents(data_dir)
	if not incidents:
		raise RadarSafetyError("没有待解除的平台风险事件")
	latest_id = str(incidents[-1]["incident_id"])
	if incident_id != latest_id:
		raise RadarSafetyError("只能确认当前最新的未解除风险事件；旧事件或未知事件不能解除")

	radar_dir = data_dir / "radar"
	radar_dir.mkdir(parents=True, exist_ok=True)
	ledger_path = radar_dir / ".safety.db"
	resolved_at = datetime.now(timezone.utc).isoformat()
	incident_ids = [str(item["incident_id"]) for item in incidents]
	try:
		with sqlite3.connect(ledger_path) as connection:
			_init_ledger(connection)
			connection.execute("BEGIN IMMEDIATE")
			connection.executemany(
				"INSERT INTO radar_incident_resolutions (incident_id, resolved_at) VALUES (?, ?)",
				((value, resolved_at) for value in incident_ids),
			)
	except sqlite3.Error as exc:
		raise RadarSafetyError(f"无法写入雷达安全事件账本: {exc}") from exc
	return {"resolved_incident_ids": incident_ids, "count": len(incident_ids), "resolved_at": resolved_at}


def _incidents_from_database(database_path: Path) -> list[dict[str, object]]:
	try:
		connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
		try:
			if not _has_table(connection, "radar_runs"):
				return []
			columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(radar_runs)")}
			required = {"run_id", "started_at", "completed_at", "status", "stats_json"}
			if not required.issubset(columns):
				raise RadarSafetyError(f"雷达运行库 schema 不完整，无法安全审计: {database_path.name}")
			rows = connection.execute(
				"""
				SELECT run_id, started_at, completed_at, stats_json
				FROM radar_runs
				WHERE status IN ('stopped', 'failed', 'running')
				   OR stats_json LIKE '%ACCOUNT_RISK%'
				   OR stats_json LIKE '%"platform_code"%'
				   OR stats_json LIKE '%"stop_code"%32%'
				   OR stats_json LIKE '%"stop_code"%36%'
				   OR stats_json LIKE '%"stop_code"%37%'
				   OR stats_json LIKE '%code%32%'
				ORDER BY COALESCE(completed_at, started_at), run_id
				LIMIT ?
				""",
				(_MAX_CANDIDATE_RUNS_PER_DATABASE + 1,),
			).fetchall()
		except sqlite3.Error as exc:
			raise RadarSafetyError(f"无法安全读取雷达运行库 {database_path.name}: {exc}") from exc
		finally:
			connection.close()
	except RadarSafetyError:
		raise
	except (OSError, ValueError, sqlite3.Error) as exc:
		raise RadarSafetyError(f"无法安全打开雷达运行库 {database_path.name}: {exc}") from exc
	if len(rows) > _MAX_CANDIDATE_RUNS_PER_DATABASE:
		raise RadarSafetyError(f"雷达风险候选记录超过安全审计上限: {database_path.name}")

	incidents: list[dict[str, object]] = []
	for run_id, started_at, completed_at, raw_stats in rows:
		if not isinstance(run_id, str) or not run_id:
			raise RadarSafetyError(f"雷达运行记录缺少 run_id: {database_path.name}")
		try:
			stats = json.loads(raw_stats)
		except (TypeError, ValueError) as exc:
			raise RadarSafetyError(f"雷达风险候选记录无法解析: {database_path.name}/{run_id}") from exc
		if not isinstance(stats, dict):
			raise RadarSafetyError(f"雷达风险候选记录不是对象: {database_path.name}/{run_id}")
		incident = _incident_from_stats(database_path.name, run_id, started_at, completed_at, stats)
		if incident is not None:
			incidents.append(incident)
	return incidents


def _incident_from_stats(
	source_database: str, run_id: str, started_at: Any, completed_at: Any, stats: dict[str, Any]
) -> dict[str, object] | None:
	stop_code = _safe_text(stats.get("stop_code"), maximum=80)
	raw_stop_reason = _safe_text(stats.get("stop_reason"), maximum=240)
	stop_reason = _safe_message(raw_stop_reason)
	handoff = stats.get("operator_handoff")
	platform_code = _safe_code(handoff.get("platform_code")) if isinstance(handoff, dict) else None
	if platform_code is None:
		platform_code = _safe_code(stats.get("platform_code"))
	if platform_code is None and stop_code in {"32", "36", "37"}:
		platform_code = int(stop_code)
	legacy_unknown_32 = stop_code.upper() == "UNKNOWN" and bool(_CODE_32_PATTERN.search(raw_stop_reason))
	if not (stop_code.upper() == _RISK_STOP_CODE or platform_code in _RISK_CODES or legacy_unknown_32):
		return None
	if legacy_unknown_32 and platform_code is None:
		platform_code = 32
	occurred_at = _safe_timestamp(completed_at) or _safe_timestamp(started_at)
	if not occurred_at:
		raise RadarSafetyError(f"风险运行缺少可信时间: {source_database}/{run_id}")
	return {
		"source_database": source_database,
		"run_id": run_id,
		"incident_id": f"{source_database}:{run_id}",
		"occurred_at": occurred_at,
		"platform_code": platform_code,
		"stop_code": stop_code or "UNKNOWN",
		"message": stop_reason or "平台风险停止；需人工确认官方访问已恢复",
	}


def _resolved_incident_ids(radar_dir: Path) -> set[str]:
	ledger_path = radar_dir / ".safety.db"
	if not ledger_path.exists():
		return set()
	try:
		with sqlite3.connect(f"{ledger_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
			if not _has_table(connection, "radar_incident_resolutions"):
				raise RadarSafetyError("雷达安全账本 schema 缺失，无法安全放行")
			columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(radar_incident_resolutions)")}
			if not {"incident_id", "resolved_at"}.issubset(columns):
				raise RadarSafetyError("雷达安全账本 schema 不完整，无法安全放行")
			rows = connection.execute("SELECT incident_id, resolved_at FROM radar_incident_resolutions").fetchall()
	except RadarSafetyError:
		raise
	except sqlite3.Error as exc:
		raise RadarSafetyError(f"无法读取雷达安全事件账本: {exc}") from exc
	resolved_ids: set[str] = set()
	for incident_id, resolved_at in rows:
		if not isinstance(incident_id, str) or not incident_id.strip() or not _safe_timestamp(resolved_at):
			raise RadarSafetyError("雷达安全账本含有不可信解除记录，无法安全放行")
		resolved_ids.add(incident_id)
	return resolved_ids


def _init_ledger(connection: sqlite3.Connection) -> None:
	connection.execute(
		"""
		CREATE TABLE IF NOT EXISTS radar_incident_resolutions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			incident_id TEXT NOT NULL,
			resolved_at TEXT NOT NULL
		)
		"""
	)


def _has_table(connection: sqlite3.Connection, table_name: str) -> bool:
	row = connection.execute(
		"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
	).fetchone()
	return row is not None


def _safe_code(value: Any) -> int | None:
	if isinstance(value, int) and not isinstance(value, bool):
		return value if -999_999 <= value <= 999_999 else None
	if isinstance(value, str) and value in {"32", "36", "37"}:
		return int(value)
	return None


def _safe_timestamp(value: Any) -> str:
	text = _safe_text(value, maximum=80)
	if not text:
		return ""
	try:
		parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return ""
	if parsed.tzinfo is None or parsed.utcoffset() is None:
		return ""
	return parsed.astimezone(timezone.utc).isoformat()


def _safe_message(value: Any) -> str:
	message = value if isinstance(value, str) else ""
	if not message or any(marker in message.lower() for marker in _SENSITIVE_MARKERS):
		return ""
	return message


def _safe_text(value: Any, *, maximum: int) -> str:
	if not isinstance(value, str):
		return ""
	text = value.strip()
	if not text or len(text) > maximum or _CONTROL_CHARS.search(text):
		return ""
	return text
