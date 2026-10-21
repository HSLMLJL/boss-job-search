"""Strict resolution of the physical runs that form one logical observation.

The list is stored in a run's JSON summary, so it must be treated as a claim
and checked against the immutable run rows before it is used for any report.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping


def resolve_observation_run_ids(
	connection: sqlite3.Connection,
	latest: Mapping[str, Any] | sqlite3.Row,
	stats: dict[str, Any],
) -> tuple[str, ...]:
	"""Return a validated physical-run chain for one logical scan.

	A record made before logical scan scopes existed is deliberately limited to
	its own physical run.  A declared multi-run scope has to prove that every
	member is from the same profile, mode, and logical scan, and that the
	declared order is a real chronological continuation.
	"""
	latest_id = _required_text(latest, "run_id", "最新扫描缺少 run_id")
	declared = stats.get("observation_run_ids")
	if declared is None and "observation_run_ids" not in stats:
		return (latest_id,)
	if not isinstance(declared, list) or not declared:
		raise ValueError("observation_run_ids 必须是非空列表")
	if any(not isinstance(run_id, str) or not run_id.strip() for run_id in declared):
		raise ValueError("observation_run_ids 含有非法运行 ID")
	run_ids = tuple(run_id.strip() for run_id in declared)
	if len(set(run_ids)) != len(run_ids):
		raise ValueError("observation_run_ids 含有重复运行 ID")
	if latest_id not in run_ids:
		raise ValueError("observation_run_ids 未包含最新运行")
	if run_ids[-1] != latest_id:
		raise ValueError("observation_run_ids 必须以最新运行作为最后一个分段")

	marks = ",".join("?" for _ in run_ids)
	rows = connection.execute(
		f"SELECT run_id, mode, status, started_at, completed_at, stats_json "
		f"FROM radar_runs WHERE run_id IN ({marks})",
		run_ids,
	).fetchall()
	by_id = {_required_text(row, "run_id", "扫描分段缺少 run_id"): row for row in rows}
	missing = [run_id for run_id in run_ids if run_id not in by_id]
	if missing:
		raise ValueError(f"observation_run_ids 包含不存在的运行：{'、'.join(missing)}")

	latest_stored_stats = _parse_stats(by_id[latest_id], latest_id)
	if latest_stored_stats != stats:
		raise ValueError("最新扫描摘要与数据库记录不一致，不能拼接分段证据")
	reference_fingerprint = _required_stats_text(
		latest_stored_stats, "profile_fingerprint", "最新扫描缺少 profile_fingerprint，不能拼接分段证据",
	)
	reference_mode = _required_text(by_id[latest_id], "mode", "最新扫描缺少 mode")
	reference_logical_id = _logical_scan_id(latest_stored_stats, latest_id)

	previous_end: datetime | None = None
	for position, run_id in enumerate(run_ids):
		row = by_id[run_id]
		segment_stats = _parse_stats(row, run_id)
		if _required_stats_text(segment_stats, "profile_fingerprint", f"分段 {run_id} 缺少 profile_fingerprint") != reference_fingerprint:
			raise ValueError(f"分段 {run_id} 的 profile_fingerprint 与最新扫描不一致")
		if _required_text(row, "mode", f"分段 {run_id} 缺少 mode") != reference_mode:
			raise ValueError(f"分段 {run_id} 的 mode 与最新扫描不一致")
		if _logical_scan_id(segment_stats, run_id) != reference_logical_id:
			raise ValueError(f"分段 {run_id} 的 logical_scan_id 与最新扫描不一致")
		started = _aware_time(row, "started_at", run_id)
		ended = _aware_time(row, "completed_at", run_id)
		if ended < started:
			raise ValueError(f"分段 {run_id} 的 completed_at 早于 started_at")
		if previous_end is not None and previous_end > started:
			raise ValueError(
				f"observation_run_ids 顺序不是连续分段：第 {position} 段 {run_id} 的开始时间早于前段结束时间"
			)
		previous_end = ended
	return run_ids


def _value(row: Mapping[str, Any] | sqlite3.Row, field: str) -> Any:
	try:
		return row[field]
	except (KeyError, IndexError):
		return None


def _required_text(row: Mapping[str, Any] | sqlite3.Row, field: str, message: str) -> str:
	value = _value(row, field)
	if not isinstance(value, str) or not value.strip():
		raise ValueError(message)
	return value.strip()


def _parse_stats(row: Mapping[str, Any] | sqlite3.Row, run_id: str) -> dict[str, Any]:
	raw = _value(row, "stats_json")
	try:
		stats = json.loads(raw) if isinstance(raw, str) else raw
	except json.JSONDecodeError as exc:
		raise ValueError(f"分段 {run_id} 的 stats_json 不是有效 JSON") from exc
	if not isinstance(stats, dict):
		raise ValueError(f"分段 {run_id} 的 stats_json 必须是对象")
	return stats


def _required_stats_text(stats: dict[str, Any], field: str, message: str) -> str:
	value = stats.get(field)
	if not isinstance(value, str) or not value.strip():
		raise ValueError(message)
	return value.strip()


def _logical_scan_id(stats: dict[str, Any], run_id: str) -> str:
	value = stats.get("logical_scan_id")
	if value is None or value == "":
		return run_id
	if not isinstance(value, str) or not value.strip():
		raise ValueError(f"分段 {run_id} 的 logical_scan_id 非法")
	return value.strip()


def _aware_time(row: Mapping[str, Any] | sqlite3.Row, field: str, run_id: str) -> datetime:
	value = _required_text(row, field, f"分段 {run_id} 缺少 {field}")
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError as exc:
		raise ValueError(f"分段 {run_id} 的 {field} 无法解析") from exc
	if parsed.tzinfo is None or parsed.utcoffset() is None:
		raise ValueError(f"分段 {run_id} 的 {field} 必须带时区")
	return parsed
