"""Read-only delivery bundle assembled from explicitly named scan sources."""

from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
import sqlite3

from boss_agent_cli.radar.delivery import _coverage_label, _load_observations, _merge_jobs
from boss_agent_cli.radar.profile import RadarProfile
from boss_agent_cli.radar.recall import (
	RecallComparisonError,
	_load_scan,
	_open_read_only,
	_validate_profile_scan,
)


class MultiDeliveryError(ValueError):
	"""Raised when a multi-source delivery manifest is not auditable."""


_CHINA_TZ = timezone(timedelta(hours=8))


def build_multi_delivery_bundle(
	*,
	main_sources: Iterable[Mapping[str, Any]],
	supplement_sources: Iterable[Mapping[str, Any]] = (),
	comparison_window_hours: float = 24,
	now: datetime | None = None,
) -> dict[str, Any]:
	"""Merge completed, non-overlapping main sources and optional probes.

	Each source is a mapping with ``db_path``, ``logical_scan_id`` and a resolved
	``RadarProfile``.  No database history outside the declared run chain is read.
	"""
	main_specs = [_source_spec(item, role="main", index=index) for index, item in enumerate(main_sources, 1)]
	supp_specs = [_source_spec(item, role="supplement", index=index) for index, item in enumerate(supplement_sources, 1)]
	if not main_specs:
		raise MultiDeliveryError("manifest 至少需要一个 main_sources 来源")
	_window_hours(comparison_window_hours)
	_unique_source_pairs((*main_specs, *supp_specs))
	_main_cities_are_disjoint(main_specs)
	_validate_supplement_scope(main_specs, supp_specs)

	main_rows: list[dict[str, Any]] = []
	supplement_rows: list[dict[str, Any]] = []
	metadata: list[dict[str, Any]] = []
	try:
		with ExitStack() as stack:
			for spec in (*main_specs, *supp_specs):
				connection = stack.enter_context(closing(_open_read_only(spec["db_path"])))
				label = spec["label"]
				scan = _load_scan(connection, spec["logical_scan_id"], label=label, strict_scope=True)
				# Any configured city suffices for the helper's membership check; it
				# then validates the complete configured city × query set.
				_validate_profile_scan(
					scan, spec["profile"], city=spec["cities"][0], label=label,
					allow_configured_window=spec["role"] == "supplement",
				)
				metadata.append(_source_metadata(connection, spec, scan))
				rows = _load_observations(
					connection, scan["run_ids"], cities=spec["cities"], queries=spec["profile"].queries,
					source=spec["role"],
					source_id=spec["source_id"], source_label=spec["label"],
					source_database=str(spec["db_path"].resolve()),
				)
				if spec["role"] == "main":
					main_rows.extend(rows)
				else:
					supplement_rows.extend(rows)
	except RecallComparisonError as exc:
		raise MultiDeliveryError(str(exc)) from exc

	time_scope = _time_scope(metadata, max_hours=comparison_window_hours, now=now)
	jobs = _merge_jobs(main_rows, supplement_rows)
	main_metadata = [item for item in metadata if item["role"] == "main"]
	supp_metadata = [item for item in metadata if item["role"] == "supplement"]
	main_cities = sorted(city for item in main_metadata for city in item["cities"])
	main_queries = sorted({query for item in main_metadata for query in item["queries"]})
	return {
		"schema_version": 1,
		**time_scope,
		"coverage": {
			"main": {
				"cities": main_cities,
				"queries": main_queries,
				"coverage_label": _aggregate_label(main_metadata, role="主扫"),
			},
			"supplement": {
				"cities": sorted({city for item in supp_metadata for city in item["cities"]}),
				"queries": sorted({query for item in supp_metadata for query in item["queries"]}),
				"coverage_label": _aggregate_label(supp_metadata, role="补扫"),
			},
			"main_sources": main_metadata,
			"supplement_sources": supp_metadata,
		},
		"merge_policy": {
			"identity": "同平台 + stable platform_job_id；缺少 ID 时才使用 identity_key",
			"no_fuzzy_deduplication": "标题或公司相似但岗位 ID 不同的记录不会合并",
			"global_newness": "本交付包不创造或声称 global new；is_global_new 固定为 null",
			"scope": "仅合并 manifest 明确列出的 run 链；不读取其他历史扫描",
			"job_field_scope": "选定 run 链决定岗位集合和观察位置；标题、薪资与AI证据等来自各库当前岗位记录，不是历史逐次快照",
		},
		"counts": {
			"unique_jobs": len(jobs),
			"main_only_jobs": sum(job["presence"] == "main_only" for job in jobs),
			"supplement_only_jobs": sum(job["presence"] == "supplement_only" for job in jobs),
			"overlap_jobs": sum(job["presence"] == "main_and_supplement" for job in jobs),
		},
		"jobs": jobs,
	}


def _source_spec(raw: Mapping[str, Any], *, role: str, index: int) -> dict[str, Any]:
	if not isinstance(raw, Mapping):
		raise MultiDeliveryError(f"{role}_sources[{index}] 必须是对象")
	profile = raw.get("profile")
	if not isinstance(profile, RadarProfile):
		raise MultiDeliveryError(f"{role}_sources[{index}].profile 必须是已加载的 RadarProfile")
	logical_scan_id = raw.get("logical_scan_id")
	if not isinstance(logical_scan_id, str) or not logical_scan_id.strip():
		raise MultiDeliveryError(f"{role}_sources[{index}].logical_scan_id 不能为空")
	db_path = raw.get("db_path")
	if not isinstance(db_path, Path):
		raise MultiDeliveryError(f"{role}_sources[{index}].db_path 必须是 Path")
	return {
		"role": role,
		"label": str(raw.get("label") or f"{'主扫' if role == 'main' else '补扫'}来源 {index}"),
		"source_id": f"{role}-{index}",
		"db_path": db_path,
		"logical_scan_id": logical_scan_id.strip(),
		"profile": profile,
		"cities": tuple(city.name for city in profile.cities),
	}


def _main_cities_are_disjoint(specs: list[dict[str, Any]]) -> None:
	owners: dict[str, str] = {}
	for spec in specs:
		for city in spec["cities"]:
			if city in owners:
				raise MultiDeliveryError(f"主扫来源城市重叠：{city} 同时属于 {owners[city]} 与 {spec['label']}")
			owners[city] = spec["label"]


def _unique_source_pairs(specs: Iterable[dict[str, Any]]) -> None:
	seen: set[tuple[Path, str]] = set()
	for spec in specs:
		pair = (spec["db_path"].resolve(), spec["logical_scan_id"])
		if pair in seen:
			raise MultiDeliveryError(f"manifest 重复引用同一数据库逻辑扫描：{pair[0]} / {pair[1]}")
		seen.add(pair)


def _validate_supplement_scope(main_specs: list[dict[str, Any]], supp_specs: list[dict[str, Any]]) -> None:
	main_cities = {city for spec in main_specs for city in spec["cities"]}
	main_searches = {(city, query) for spec in main_specs for city in spec["cities"] for query in spec["profile"].queries}
	supplement_searches: set[tuple[str, str]] = set()
	for spec in supp_specs:
		outside = set(spec["cities"]) - main_cities
		if outside:
			raise MultiDeliveryError(f"补扫来源城市不在主扫范围内：{'、'.join(sorted(outside))}")
		duplicate = main_searches & {(city, query) for city in spec["cities"] for query in spec["profile"].queries}
		if duplicate:
			formatted = "、".join(f"{city} / {query}" for city, query in sorted(duplicate))
			raise MultiDeliveryError(f"补扫与主扫存在同城市同词重复：{formatted}")
		own_searches = {(city, query) for city in spec["cities"] for query in spec["profile"].queries}
		if own_searches & supplement_searches:
			formatted = "、".join(f"{city} / {query}" for city, query in sorted(own_searches & supplement_searches))
			raise MultiDeliveryError(f"补扫来源存在同城市同词重叠：{formatted}")
		supplement_searches.update(own_searches)


def _source_metadata(connection: sqlite3.Connection, spec: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
	stats = scan["runs"][-1]["stats"]
	actual = _actual_capture_metrics(connection, scan["run_ids"], spec["cities"], spec["profile"].queries, scan["runs"])
	return {
		"source_id": spec["source_id"], "role": spec["role"], "label": spec["label"], "database": str(spec["db_path"].resolve()),
		"logical_scan_id": spec["logical_scan_id"], "profile_name": spec["profile"].name,
		"profile_fingerprint": spec["profile"].fingerprint, "cities": list(spec["cities"]),
		"queries": list(spec["profile"].queries), "run_ids": list(scan["run_ids"]),
		"run_modes": [run["mode"] for run in scan["runs"]],
		"started_at": scan["runs"][0]["started_at"], "completed_at": scan["runs"][-1]["completed_at"],
		"time_zone": "UTC（run 原始记录）",
		"coverage_label": _coverage_label(stats, cities=spec["cities"], queries=spec["profile"].queries, role=spec["label"]),
		"page_cap_reached_searches": list(stats.get("page_cap_reached_searches") or []),
		"city_page_limits": spec["profile"].city_page_limits,
		"search_page_limits": spec["profile"].search_page_limits,
		"query_specific_page_limits": bool(spec["profile"].query_page_limits),
		"terminal_full_searches": list(stats.get("terminal_full_searches") or []),
		"terminal_evidence_missing_searches": list(stats.get("terminal_evidence_missing_searches") or []),
		"complete": bool(stats.get("complete")),
		**actual,
	}


def _actual_capture_metrics(
	connection: sqlite3.Connection,
	run_ids: Iterable[str],
	cities: Iterable[str],
	queries: Iterable[str],
	runs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
	"""Summarize only raw capture/observation rows in the validated run chain.

	Profile page limits are deliberately absent from this calculation: a limit is
	a guardrail, not evidence that that many platform requests or pages occurred.
	"""
	run_values, city_values, query_values = tuple(run_ids), tuple(cities), tuple(queries)
	expected = [(city, query) for city in city_values for query in query_values]
	request_values = [_nonnegative_int_or_none(run["stats"].get("requests")) for run in runs]
	actual_requests = sum(value for value in request_values if value is not None) if all(
		value is not None for value in request_values
	) else None
	if not run_values or not expected:
		return {
			"actual_requests": actual_requests, "actual_batches": 0, "actual_raw_jobs": 0,
			"latest_contiguous_capture_batches": 0, "latest_contiguous_raw_jobs": 0,
			"total_capture_batches": 0, "total_raw_jobs": 0, "capture_restart_detected": False,
			"capture_scope": "entire_chain_continuous",
			"relevant_unique_jobs": 0, "page_capture_evidence_available": True,
			"page_capture_by_city_query": [],
		}
	marks = ",".join("?" for _ in run_values)
	try:
		capture_rows = connection.execute(
			f"SELECT run_id, shard, query, page, raw_job_count, has_more, page_size "
			f"FROM radar_page_captures WHERE run_id IN ({marks})",
			run_values,
		).fetchall()
	except sqlite3.OperationalError:
		capture_rows = None

	observation_rows = connection.execute(
		f"SELECT COUNT(DISTINCT o.job_pk) FROM radar_run_observations o "
		f"WHERE o.run_id IN ({marks}) AND o.shard IN ({','.join('?' for _ in city_values)}) "
		f"AND o.query IN ({','.join('?' for _ in query_values)})",
		(*run_values, *city_values, *query_values),
	).fetchone()
	relevant_unique = int(observation_rows[0]) if observation_rows else 0
	if capture_rows is None:
		return {
			"actual_requests": actual_requests, "actual_batches": None, "actual_raw_jobs": None,
			"latest_contiguous_capture_batches": None, "latest_contiguous_raw_jobs": None,
			"total_capture_batches": None, "total_raw_jobs": None, "capture_restart_detected": None,
			"capture_scope": "raw_capture_table_unavailable",
			"relevant_unique_jobs": relevant_unique, "page_capture_evidence_available": False,
			"page_capture_by_city_query": [
				{"city": city, "query": query, "capture_status": "table_unavailable", "actual_batches": None,
				 "actual_raw_jobs": None, "tail_has_more": None, "tail_raw_job_count": None,
				 "tail_page_size": None, "continuous_from_page_1": None}
				for city, query in expected
			],
		}

	positions = {run_id: index for index, run_id in enumerate(run_values)}
	by_search: dict[tuple[str, str], list[sqlite3.Row]] = {}
	all_by_search: dict[tuple[str, str], list[sqlite3.Row]] = {}
	restarted: set[tuple[str, str]] = set()
	for row in sorted(capture_rows, key=lambda item: (positions[str(item["run_id"])], str(item["shard"]), str(item["query"]), int(item["page"]))):
		key = (str(row["shard"]), str(row["query"]))
		if key not in expected:
			continue
		all_by_search.setdefault(key, []).append(row)
		pages = by_search.setdefault(key, [])
		if pages and int(row["page"]) <= int(pages[-1]["page"]):
			# A resumed segment restarted this search; its old tail is not a
			# continuation of the current recorded capture sequence.
			pages.clear()
			restarted.add(key)
		pages.append(row)

	summaries: list[dict[str, Any]] = []
	for city, query in expected:
		pages = by_search.get((city, query), [])
		total_pages = all_by_search.get((city, query), [])
		if not pages:
			summaries.append({"city": city, "query": query, "capture_status": "missing", "actual_batches": 0,
				"actual_raw_jobs": 0, "tail_has_more": None, "tail_raw_job_count": None,
				"tail_page_size": None, "continuous_from_page_1": False, "restart_detected": False,
				"total_capture_batches": len(total_pages), "total_raw_jobs": sum(int(item["raw_job_count"]) for item in total_pages)})
			continue
		page_numbers = [int(item["page"]) for item in pages]
		tail = pages[-1]
		summaries.append({
			"city": city, "query": query, "capture_status": "recorded",
			"actual_batches": len(pages), "actual_raw_jobs": sum(int(item["raw_job_count"]) for item in pages),
			"tail_has_more": bool(tail["has_more"]), "tail_raw_job_count": int(tail["raw_job_count"]),
			"tail_page_size": int(tail["page_size"]) if tail["page_size"] is not None else None,
			"continuous_from_page_1": page_numbers == list(range(1, page_numbers[-1] + 1)),
			"restart_detected": (city, query) in restarted,
			"total_capture_batches": len(total_pages),
			"total_raw_jobs": sum(int(item["raw_job_count"]) for item in total_pages),
		})
	restart_detected = any(item["restart_detected"] for item in summaries)
	return {
		"actual_requests": actual_requests,
		"actual_batches": sum(item["actual_batches"] for item in summaries),
		"actual_raw_jobs": sum(item["actual_raw_jobs"] for item in summaries),
		"latest_contiguous_capture_batches": sum(item["actual_batches"] for item in summaries),
		"latest_contiguous_raw_jobs": sum(item["actual_raw_jobs"] for item in summaries),
		"total_capture_batches": sum(item["total_capture_batches"] for item in summaries),
		"total_raw_jobs": sum(item["total_raw_jobs"] for item in summaries),
		"capture_restart_detected": restart_detected,
		"capture_scope": "latest_contiguous_segment_after_restart" if restart_detected else "entire_chain_continuous",
		"relevant_unique_jobs": relevant_unique,
		"page_capture_evidence_available": True,
		"page_capture_by_city_query": summaries,
	}


def _nonnegative_int_or_none(value: Any) -> int | None:
	return value if isinstance(value, int) and value >= 0 else None


def _aggregate_label(metadata: list[dict[str, Any]], *, role: str) -> str:
	if not metadata:
		return f"未配置{role}来源。"
	parts = []
	for source in metadata:
		page_limits = source["search_page_limits"] if source.get("query_specific_page_limits") else source["city_page_limits"]
		limits = "、".join(f"{search}:{pages}批" for search, pages in sorted(page_limits.items()))
		boundary = []
		if source["page_cap_reached_searches"]:
			boundary.append(f"{len(source['page_cap_reached_searches'])} 个达到配置批次")
		if source["terminal_full_searches"]:
			boundary.append(f"{len(source['terminal_full_searches'])} 个终批满载")
		if source["terminal_evidence_missing_searches"]:
			boundary.append(f"{len(source['terminal_evidence_missing_searches'])} 个缺终点证据")
		parts.append(
			f"{source['label']}（{'、'.join(source['cities'])}；词：{'、'.join(source['queries'])}；"
			f"保护上限：{limits or '未声明'}；实际采集：{_actual_text(source)}；"
			f"{'、'.join(boundary) or '未报告满批或缺证据'}）"
		)
	limit = "窗口不代表自然终点。" if role == "补扫" else "页面停止不代表城市岗位已搜全。"
	return f"{role}实际来源：" + "；".join(parts) + f"。{limit}"


def _actual_text(source: dict[str, Any]) -> str:
	if not source["page_capture_evidence_available"]:
		return f"原始批次未留存；请求 {source['actual_requests'] if source['actual_requests'] is not None else '未知'}"
	requests = source["actual_requests"] if source["actual_requests"] is not None else "未知"
	if source["capture_restart_detected"]:
		return (
			f"全链记录 {source['total_capture_batches']} 批、原始 {source['total_raw_jobs']} 条；"
			f"最近连续段 {source['latest_contiguous_capture_batches']} 批、原始 "
			f"{source['latest_contiguous_raw_jobs']} 条（检测到分页重启）；相关唯一 "
			f"{source['relevant_unique_jobs']}、请求 {requests}"
		)
	return (
		f"{source['actual_batches']} 批、原始 {source['actual_raw_jobs']} 条、"
		f"相关唯一 {source['relevant_unique_jobs']}、请求 {requests}"
	)


def _window_hours(value: float) -> None:
	if not isinstance(value, (int, float)) or not 0 < float(value) <= 24:
		raise MultiDeliveryError("comparison_window_hours 必须在 0–24 小时之间")


def _time_scope(metadata: list[dict[str, Any]], *, max_hours: float, now: datetime | None) -> dict[str, Any]:
	now_value = now or datetime.now(timezone.utc)
	if now_value.tzinfo is None or now_value.utcoffset() is None:
		raise MultiDeliveryError("now 必须带时区")
	starts: list[datetime] = []
	ends: list[datetime] = []
	for source in metadata:
		for field, destination in (("started_at", starts), ("completed_at", ends)):
			try:
				value = datetime.fromisoformat(str(source[field]).replace("Z", "+00:00"))
			except ValueError as exc:
				raise MultiDeliveryError(f"{source['label']} 的 {field} 无法解析") from exc
			if value.tzinfo is None or value.utcoffset() is None:
				raise MultiDeliveryError(f"{source['label']} 的 {field} 必须带时区")
			if value > now_value:
				raise MultiDeliveryError(f"{source['label']} 存在未来时间片段：{field}")
			destination.append(value)
	if not starts or not ends:
		raise MultiDeliveryError("没有可用来源时间范围")
	start, end = min(starts), max(ends)
	if end < start or end - start > timedelta(hours=float(max_hours)):
		raise MultiDeliveryError(f"来源时间范围超过设定 {max_hours:g} 小时")
	dates = sorted({value.astimezone(_CHINA_TZ).date().isoformat() for value in (*starts, *ends)})
	china_start, china_end = start.astimezone(_CHINA_TZ), end.astimezone(_CHINA_TZ)
	return {
		"scan_date": dates[0] if len(dates) == 1 else None,
		"scan_window": {"start_at": china_start.isoformat(), "end_at": china_end.isoformat(), "dates": dates,
			"crosses_midnight": len(dates) > 1, "max_hours": float(max_hours),
			"timezone": "Asia/Shanghai", "mode": "explicit_multi_source_bounded_window",
			"limitations": "仅为 manifest 指定来源的采集时间边界；不代表全量或全局新增。"},
	}
