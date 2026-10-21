"""Read-only exact-stable-ID historical enrichment for delivery bundles."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from boss_agent_cli.radar.models import JobObservation, is_relevant, normalize_relevance_mode

_CHINA_TZ = timezone(timedelta(hours=8))
_REQUIRED_COLUMNS = {"platform", "platform_job_id", "title", "payload_json", "first_seen_at", "first_run_id"}
_SQLITE_IN_CHUNK_SIZE = 500
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_KNOWN_STATUSES = {"known_old", "first_observed_today"}
_UNKNOWN_STATUSES = {
	"no_stable_platform_id",
	"ambiguous_anchor_platform_id",
	"anchor_history_not_ai_pm_relevant",
	"anchor_first_seen_invalid",
}
_ALL_STATUSES = _KNOWN_STATUSES | _UNKNOWN_STATUSES | {"absent_from_anchor_at_cutoff"}


class HistoryAnchorError(ValueError):
	"""The bundle or anchor cannot support a safe comparison."""


def validate_history_anchor_annotations(bundle: Mapping[str, Any]) -> None:
	"""Validate renderer-facing annotations without reopening source databases."""
	if not isinstance(bundle, Mapping) or not isinstance(bundle.get("jobs"), list):
		raise HistoryAnchorError("bundle.jobs must be a list")
	metadata = bundle.get("history_anchor")
	if not isinstance(metadata, Mapping):
		raise HistoryAnchorError("history_anchor metadata is required")
	for field in ("database_sha256", "schema_sha256", "queried_content_sha256"):
		if not _is_sha256(metadata.get(field)):
			raise HistoryAnchorError(f"history_anchor.{field} must be a lowercase SHA-256")
	for field in ("lookup_observed_at", "delivery_window_end_at", "requested_cutoff_at", "effective_cutoff_at"):
		if _parse_timestamp(metadata.get(field)) is None:
			raise HistoryAnchorError(f"history_anchor.{field} must be timezone-aware")
	delivery_cutoff = _parse_timestamp(metadata["delivery_window_end_at"])
	requested_cutoff = _parse_timestamp(metadata["requested_cutoff_at"])
	effective_cutoff = _parse_timestamp(metadata["effective_cutoff_at"])
	assert delivery_cutoff and requested_cutoff and effective_cutoff
	if delivery_cutoff != _delivery_cutoff(bundle):
		raise HistoryAnchorError("history_anchor delivery cutoff does not match bundle.scan_window.end_at")
	if effective_cutoff != min(delivery_cutoff, requested_cutoff):
		raise HistoryAnchorError("history_anchor effective cutoff is inconsistent with delivery/requested cutoffs")
	for field in (
		"database",
		"snapshot_consistency",
		"cutoff_semantics",
		"absence_semantics",
		"relevance_evidence_scope",
	):
		if not isinstance(metadata.get(field), str) or not metadata[field]:
			raise HistoryAnchorError("history_anchor provenance and semantic notes are required")
	_validate_sources(metadata.get("sources"))

	actual: Counter[str] = Counter()
	for job in bundle["jobs"]:
		if not isinstance(job, Mapping):
			raise HistoryAnchorError("bundle.jobs entries must be objects")
		status = job.get("anchor_history_status")
		if status not in _ALL_STATUSES:
			raise HistoryAnchorError("job anchor_history_status is missing or unsupported")
		actual[status] += 1
		known, first_seen, first_run = (
			job.get("known_in_anchor_history"),
			job.get("anchor_first_seen_at"),
			job.get("anchor_first_run_id"),
		)
		if status in _KNOWN_STATUSES:
			if not str(job.get("platform") or "") or not str(job.get("platform_job_id") or ""):
				raise HistoryAnchorError(f"{status} requires a nonempty platform and stable platform job ID")
			parsed = _parse_timestamp(first_seen)
			if (
				known is not True
				or not isinstance(first_run, str)
				or not first_run.strip()
				or parsed is None
				or parsed > effective_cutoff
			):
				raise HistoryAnchorError(
					f"{status} requires known=true and valid first-seen evidence at or before cutoff"
				)
			first_day, cutoff_day = parsed.astimezone(_CHINA_TZ).date(), effective_cutoff.astimezone(_CHINA_TZ).date()
			if status == "first_observed_today" and first_day != cutoff_day:
				raise HistoryAnchorError("first_observed_today must use the effective cutoff's China date")
			if status == "known_old" and first_day >= cutoff_day:
				raise HistoryAnchorError("known_old must predate the effective cutoff's China date")
		elif status == "absent_from_anchor_at_cutoff":
			if known is not False or first_seen is not None or first_run is not None:
				raise HistoryAnchorError("absent_from_anchor_at_cutoff requires known=false and null anchor fields")
		elif known is not None or first_seen is not None or first_run is not None:
			raise HistoryAnchorError(f"{status} requires unknown history and null anchor fields")
	counts = metadata.get("counts")
	if not isinstance(counts, Mapping):
		raise HistoryAnchorError("history_anchor.counts must be an object")
	declared = dict(counts)
	if any(
		status not in _ALL_STATUSES or isinstance(count, bool) or not isinstance(count, int) or count < 0
		for status, count in declared.items()
	):
		raise HistoryAnchorError("history_anchor.counts contains an invalid status or count")
	if declared != dict(sorted(actual.items())):
		raise HistoryAnchorError("history_anchor.counts does not exactly match job annotation statuses")


def enrich_bundle_with_history_anchor(
	bundle: Mapping[str, Any],
	*,
	anchor_db_path: Path | None = None,
	anchor_db_paths: Sequence[Path] | None = None,
	anchor_cutoff_at: datetime | str | None = None,
	relevance_mode: str = "ai_pm",
) -> dict[str, Any]:
	"""Annotate from one or more explicit read-only anchors.

	Cross-database repeats are merged by earliest valid, relevant first_seen_at.
	Duplicate rows in one database, bad timestamps, and cutoff-bounded irrelevant
	evidence are intentionally uncertain rather than guessed away.
	"""
	if not isinstance(bundle, Mapping):
		raise HistoryAnchorError("bundle must be a mapping")
	relevance_mode = normalize_relevance_mode(relevance_mode)
	result = copy.deepcopy(dict(bundle))
	jobs = result.get("jobs")
	if not isinstance(jobs, list):
		raise HistoryAnchorError("bundle.jobs must be a list")
	delivery_cutoff = _delivery_cutoff(bundle)
	requested_cutoff = _parse_timestamp(anchor_cutoff_at) if anchor_cutoff_at is not None else delivery_cutoff
	if requested_cutoff is None:
		raise HistoryAnchorError("anchor_cutoff_at must be timezone-aware")
	effective_cutoff = min(requested_cutoff, delivery_cutoff)
	snapshots = [
		_read_anchor_snapshot(path, _bundle_ids(jobs))
		for path in _normalise_anchor_paths(anchor_db_path, anchor_db_paths)
	]
	grouped: dict[tuple[str, str], list[tuple[dict[str, str], Mapping[str, str], bool]]] = defaultdict(list)
	for snapshot in snapshots:
		per_source: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
		for row in snapshot["rows"]:
			per_source[(row["platform"], row["platform_job_id"])].append(row)
		for key, rows in per_source.items():
			grouped[key].extend((row, snapshot["evidence"], len(rows) > 1) for row in rows)

	counts: Counter[str] = Counter()
	for job in jobs:
		if not isinstance(job, dict):
			raise HistoryAnchorError("bundle.jobs entries must be objects")
		platform, job_id = str(job.get("platform") or ""), str(job.get("platform_job_id") or "")
		if not platform or not job_id:
			_annotate(job, "no_stable_platform_id", None)
			counts["no_stable_platform_id"] += 1
			continue
		candidates = grouped.get((platform, job_id), [])
		if any(duplicate for _row, _source, duplicate in candidates):
			_annotate(job, "ambiguous_anchor_platform_id", None)
			counts["ambiguous_anchor_platform_id"] += 1
			continue
		if not candidates:
			_annotate(job, "absent_from_anchor_at_cutoff", False)
			counts["absent_from_anchor_at_cutoff"] += 1
			continue
		eligible: list[tuple[datetime, dict[str, str], Mapping[str, str]]] = []
		invalid, irrelevant = False, False
		for row, source, _duplicate in candidates:
			first_seen = _parse_timestamp(row["first_seen_at"])
			if first_seen is None:
				invalid = True
				continue
			if first_seen > effective_cutoff:
				continue
			if not _anchor_row_is_relevant(row, relevance_mode=relevance_mode):
				irrelevant = True
				continue
			eligible.append((first_seen, row, source))
		if invalid:
			_annotate(job, "anchor_first_seen_invalid", None)
			counts["anchor_first_seen_invalid"] += 1
			continue
		if irrelevant:
			_annotate(job, "anchor_history_not_ai_pm_relevant", None)
			counts["anchor_history_not_ai_pm_relevant"] += 1
			continue
		if not eligible:
			_annotate(job, "absent_from_anchor_at_cutoff", False)
			counts["absent_from_anchor_at_cutoff"] += 1
			continue
		eligible.sort(key=lambda item: (item[0], item[2]["database"], item[1]["first_run_id"]))
		first_seen, row, _source = eligible[0]
		status = (
			"first_observed_today"
			if first_seen.astimezone(_CHINA_TZ).date() == effective_cutoff.astimezone(_CHINA_TZ).date()
			else "known_old"
		)
		_annotate(job, status, True, row["first_seen_at"], row["first_run_id"])
		job["anchor_history_evidence"] = [
			{
				"database": source["database"],
				"first_seen_at": candidate["first_seen_at"],
				"first_run_id": candidate["first_run_id"],
			}
			for _seen, candidate, source in eligible
		]
		counts[status] += 1

	sources = [snapshot["evidence"] for snapshot in snapshots]
	result["history_anchor"] = {
		**_aggregate_evidence(sources),
		"sources": sources,
		"lookup_observed_at": datetime.now(timezone.utc).isoformat(),
		"delivery_window_end_at": delivery_cutoff.isoformat(),
		"requested_cutoff_at": requested_cutoff.isoformat(),
		"relevance_mode": relevance_mode,
		"effective_cutoff_at": effective_cutoff.isoformat(),
		"cutoff_semantics": "只承认锚点库在交付窗口结束前已首次观察到的精确稳定岗位ID；不是发布时间，也不证明平台全量。",
		"absence_semantics": "锚点未命中只表示截至截止时间该锚点库没有该精确ID，绝不表示平台新发布。",
		"relevance_evidence_scope": "锚点 radar_jobs 当前可见的 title 与 payload_json，用于防止历史不相关记录否定当前岗位；不是逐次历史观察快照。",
		"counts": dict(sorted(counts.items())),
	}
	validate_history_anchor_annotations(result)
	return result


def _normalise_anchor_paths(single: Path | None, multiple: Sequence[Path] | None) -> list[Path]:
	if single is not None and multiple is not None:
		raise HistoryAnchorError("use anchor_db_path or anchor_db_paths, not both")
	raw = multiple if multiple is not None else (() if single is None else (single,))
	if not raw:
		raise HistoryAnchorError("at least one anchor database is required")
	paths = [Path(path).resolve() for path in raw]
	if len(paths) != len(set(paths)):
		raise HistoryAnchorError("anchor databases must be distinct explicit paths")
	if missing := next((path for path in paths if not path.is_file()), None):
		raise HistoryAnchorError(f"anchor database does not exist: {missing}")
	return paths


def _read_anchor_snapshot(path: Path, ids_by_platform: Mapping[str, set[str]]) -> dict[str, Any]:
	connection = _open_read_only(path)
	try:
		connection.execute("BEGIN")
		schema_sha256 = _validate_anchor_schema(connection)
		rows = _load_anchor_rows(connection, ids_by_platform)
		queried_content_sha256 = _queried_content_sha256(schema_sha256, rows)
		connection.execute("COMMIT")
	finally:
		connection.close()
	return {
		"rows": rows,
		"evidence": {
			"database": str(path),
			"database_sha256": _file_sha256(path),
			"schema_sha256": schema_sha256,
			"queried_content_sha256": queried_content_sha256,
			"snapshot_consistency": "SQLite read-only transaction; queried_content_sha256 binds the schema and exact rows used, including WAL-visible snapshot state.",
		},
	}


def _aggregate_evidence(sources: list[Mapping[str, str]]) -> dict[str, str]:
	if len(sources) == 1:
		return dict(sources[0])

	def digest(field: str) -> str:
		payload = [{"database": source["database"], field: source[field]} for source in sources]
		return hashlib.sha256(
			json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
		).hexdigest()

	return {
		"database": f"{len(sources)} explicit read-only anchor databases",
		"database_sha256": digest("database_sha256"),
		"schema_sha256": digest("schema_sha256"),
		"queried_content_sha256": digest("queried_content_sha256"),
		"snapshot_consistency": "Each source was read in its own SQLite read-only transaction; sources records the exact per-database snapshot evidence.",
	}


def _validate_sources(sources: Any) -> None:
	if sources is None:
		return
	if not isinstance(sources, list) or not sources:
		raise HistoryAnchorError("history_anchor.sources must be a nonempty evidence list")
	seen: set[str] = set()
	for source in sources:
		if (
			not isinstance(source, Mapping)
			or not isinstance(source.get("database"), str)
			or not source["database"]
			or source["database"] in seen
		):
			raise HistoryAnchorError("history_anchor.sources requires distinct database paths")
		seen.add(source["database"])
		if any(
			not _is_sha256(source.get(field))
			for field in ("database_sha256", "schema_sha256", "queried_content_sha256")
		):
			raise HistoryAnchorError("history_anchor.sources hashes must be lowercase SHA-256")
		if not isinstance(source.get("snapshot_consistency"), str) or not source["snapshot_consistency"]:
			raise HistoryAnchorError("history_anchor.sources snapshot consistency is required")


def _delivery_cutoff(bundle: Mapping[str, Any]) -> datetime:
	scan_window = bundle.get("scan_window")
	cutoff = _parse_timestamp(scan_window.get("end_at")) if isinstance(scan_window, Mapping) else None
	if cutoff is None:
		raise HistoryAnchorError("bundle.scan_window.end_at must be timezone-aware")
	return cutoff


def _bundle_ids(jobs: list[Any]) -> dict[str, set[str]]:
	result: dict[str, set[str]] = defaultdict(set)
	for job in jobs:
		if not isinstance(job, Mapping):
			raise HistoryAnchorError("bundle.jobs entries must be objects")
		platform, job_id = str(job.get("platform") or ""), str(job.get("platform_job_id") or "")
		if platform and job_id:
			result[platform].add(job_id)
	return result


def _open_read_only(path: Path) -> sqlite3.Connection:
	connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
	connection.execute("PRAGMA query_only = ON")
	return connection


def _validate_anchor_schema(connection: sqlite3.Connection) -> str:
	columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(radar_jobs)")}
	missing = sorted(_REQUIRED_COLUMNS - columns)
	if missing:
		raise HistoryAnchorError(f"anchor radar_jobs schema missing columns: {', '.join(missing)}")
	row = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'radar_jobs'").fetchone()
	if not row or not row[0]:
		raise HistoryAnchorError("anchor radar_jobs table definition is unavailable")
	return hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest()


def _load_anchor_rows(connection: sqlite3.Connection, ids: Mapping[str, set[str]]) -> list[dict[str, str]]:
	rows: list[dict[str, str]] = []
	for platform, job_ids in ids.items():
		values = sorted(job_ids)
		for start in range(0, len(values), _SQLITE_IN_CHUNK_SIZE):
			chunk = values[start : start + _SQLITE_IN_CHUNK_SIZE]
			if not chunk:
				continue
			marks = ",".join("?" for _ in chunk)
			for row in connection.execute(
				f"SELECT platform, platform_job_id, title, payload_json, first_seen_at, first_run_id FROM radar_jobs WHERE platform = ? AND platform_job_id IN ({marks})",
				(platform, *chunk),
			):
				rows.append(
					{
						"platform": str(row[0]),
						"platform_job_id": str(row[1]),
						"title": str(row[2]),
						"payload_json": str(row[3]),
						"first_seen_at": str(row[4]),
						"first_run_id": str(row[5]),
					}
				)
	return rows


def _queried_content_sha256(schema_sha256: str, rows: list[dict[str, str]]) -> str:
	payload = {
		"schema_sha256": schema_sha256,
		"rows": sorted(rows, key=lambda row: tuple(row[key] for key in sorted(row))),
	}
	return hashlib.sha256(
		json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
	).hexdigest()


def _anchor_row_is_relevant(
	row: Mapping[str, str], *, relevance_mode: str = "ai_pm",
) -> bool:
	try:
		payload = json.loads(row["payload_json"])
		if not isinstance(payload, dict):
			return False
		stored_mode = normalize_relevance_mode(str(payload.get("relevance_mode") or "ai_pm"))
		if stored_mode != relevance_mode:
			return False
		terms = payload.get("relevance_evidence")
		if not isinstance(terms, list):
			terms = payload.get("ai_evidence", [])
		return isinstance(terms, list) and is_relevant(
				JobObservation(
						platform=str(row["platform"]),
						query="",
						shard="",
						title=str(row["title"]),
						company="",
						city="",
						description=" ".join(str(term) for term in terms),
				),
				relevance_mode=relevance_mode,
		)
	except (TypeError, ValueError):
		return False


def _annotate(
	job: dict[str, Any],
	status: str,
	known: bool | None,
	first_seen_at: str | None = None,
	first_run_id: str | None = None,
) -> None:
	job["known_in_anchor_history"], job["anchor_history_status"] = known, status
	job["anchor_first_seen_at"], job["anchor_first_run_id"] = first_seen_at, first_run_id
	job["anchor_history_scope"] = (
		"exact stable platform_job_id in named read-only anchor, bounded by delivery cutoff; not platform publication"
	)


def _parse_timestamp(value: datetime | str | Any) -> datetime | None:
	try:
		parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
	except (TypeError, ValueError):
		return None
	return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _is_sha256(value: Any) -> bool:
	return isinstance(value, str) and bool(_SHA256_PATTERN.fullmatch(value))


def _file_sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()
