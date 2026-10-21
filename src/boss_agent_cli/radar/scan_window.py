"""Honest bounded rolling windows for offline radar comparison reports.

The normal report path remains calendar-day strict.  This helper is for an
explicitly selected, short rolling comparison window when a complete scan and
its supplemental probe legitimately straddle midnight.  It validates time
coverage only; it does not change collection, deduplication, or event labels.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from numbers import Real
from typing import Any, Mapping, Sequence


_CHINA_TZ = timezone(timedelta(hours=8))
_TIMEZONE_LABEL = "Asia/Shanghai (UTC+08:00)"


class ScanWindowError(ValueError):
	"""Raised when scan sessions cannot form one bounded rolling window."""


def validate_rolling_scan_window(
	main_scan: Mapping[str, Any],
	supplement_scan: Mapping[str, Any],
	*,
	max_hours: float,
) -> dict[str, Any]:
	"""Validate a sequential main-plus-supplement rolling report window.

	This is intentionally stricter than merely comparing final timestamps: every
	physical run must have aware start/end values, segments inside each logical
	scan cannot overlap, and all supplement work must begin after main work ends.
	"""
	limit_hours = _validate_max_hours(max_hours)
	main_segments = _parse_scan_segments(main_scan, label="主扫描")
	supplement_segments = _parse_scan_segments(supplement_scan, label="补充扫描")
	_validate_non_overlapping(main_segments, label="主扫描")
	_validate_non_overlapping(supplement_segments, label="补充扫描")

	main_start = min(segment[1] for segment in main_segments)
	main_end = max(segment[2] for segment in main_segments)
	supplement_start = min(segment[1] for segment in supplement_segments)
	supplement_end = max(segment[2] for segment in supplement_segments)
	if supplement_start < main_end:
		raise ScanWindowError("补充扫描必须在主扫描全部结束后才可开始")

	span = supplement_end - main_start
	if span > timedelta(hours=limit_hours):
		raise ScanWindowError(
			f"主扫描与补充扫描合计跨度为 {span.total_seconds() / 3600:.2f} 小时，"
			f"超过滚动窗口上限 {limit_hours:g} 小时"
		)

	start_at = main_start.astimezone(_CHINA_TZ)
	end_at = supplement_end.astimezone(_CHINA_TZ)
	dates = _inclusive_china_dates(start_at.date(), end_at.date())
	crosses_midnight = len(dates) > 1
	return {
		"mode": "rolling",
		"max_hours": limit_hours,
		"start_at": start_at.isoformat(),
		"end_at": end_at.isoformat(),
		"crosses_midnight": crosses_midnight,
		"dates": [value.isoformat() for value in dates],
		"timezone": _TIMEZONE_LABEL,
		"label": "滚动扫描窗口（非自然日口径）",
		"limitations": (
			"该结果仅代表这个有限滚动窗口内完成的主扫描与补充扫描；"
			"不得当作‘今日新增’，也不得当作平台全量召回率。"
		),
	}


def _validate_max_hours(value: float) -> float:
	if isinstance(value, bool) or not isinstance(value, Real):
		raise ScanWindowError("max_hours 必须是大于 0 且不超过 24 的有限实数")
	result = float(value)
	if not math.isfinite(result) or not 0 < result <= 24:
		raise ScanWindowError("max_hours 必须是大于 0 且不超过 24 的有限实数")
	return result


def _parse_scan_segments(scan: Mapping[str, Any], *, label: str) -> list[tuple[str, datetime, datetime]]:
	if not isinstance(scan, Mapping):
		raise ScanWindowError(f"{label}缺少扫描结构")
	runs = scan.get("runs")
	if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)) or not runs:
		raise ScanWindowError(f"{label}必须包含至少一个分段")
	segments: list[tuple[str, datetime, datetime]] = []
	for index, run in enumerate(runs, start=1):
		if not isinstance(run, Mapping):
			raise ScanWindowError(f"{label}分段 {index} 不是对象")
		run_id = str(run.get("run_id") or index)
		start = _parse_aware_timestamp(run.get("started_at"), label=label, run_id=run_id, field="started_at")
		end = _parse_aware_timestamp(run.get("completed_at"), label=label, run_id=run_id, field="completed_at")
		if end < start:
			raise ScanWindowError(f"{label}分段 {run_id} 的完成时间早于开始时间")
		segments.append((run_id, start, end))
	return segments


def _parse_aware_timestamp(value: Any, *, label: str, run_id: str, field: str) -> datetime:
	try:
		parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
	except (TypeError, ValueError) as exc:
		raise ScanWindowError(f"{label}分段 {run_id} 缺少可解析且带时区的 {field}") from exc
	if parsed.tzinfo is None or parsed.utcoffset() is None:
		raise ScanWindowError(f"{label}分段 {run_id} 的 {field} 必须带时区")
	return parsed


def _validate_non_overlapping(segments: list[tuple[str, datetime, datetime]], *, label: str) -> None:
	previous: tuple[str, datetime, datetime] | None = None
	for segment in sorted(segments, key=lambda item: (item[1], item[2], item[0])):
		if previous is not None and segment[1] < previous[2]:
			raise ScanWindowError(f"{label}分段 {previous[0]} 与 {segment[0]} 时间重叠")
		previous = segment


def _inclusive_china_dates(start: date, end: date) -> list[date]:
	values: list[date] = []
	current = start
	while current <= end:
		values.append(current)
		current += timedelta(days=1)
	return values
