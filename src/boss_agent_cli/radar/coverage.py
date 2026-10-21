"""Read-only coverage evidence, independent of filtered job counts and Excel."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from boss_agent_cli.radar.acceptance import (
	_natural_end_page_evidence,
	_read_only_connection,
	_table_exists,
)
from boss_agent_cli.radar.profile import RadarProfile
from boss_agent_cli.radar.evidence_scope import resolve_observation_run_ids


def build_coverage_report(database_path: Path, profile: RadarProfile) -> dict[str, Any]:
	"""Audit the latest logical scan; never infer raw rows from filtered rows."""
	database_path = database_path.resolve()
	if not database_path.is_file():
		raise ValueError(f"雷达数据库不存在：{database_path}")
	expected = {f"{city.name} / {query}" for city in profile.cities for query in profile.queries}
	with closing(_read_only_connection(database_path)) as connection:
		latest = connection.execute(
			"SELECT run_id, mode, status, completed_at, started_at, stats_json FROM radar_runs "
			"ORDER BY started_at DESC, run_id DESC LIMIT 1"
		).fetchone()
		if latest is None:
			raise ValueError("数据库中没有扫描记录")
		stats = json.loads(latest["stats_json"])
		if not isinstance(stats, dict):
			raise ValueError("扫描摘要不是有效对象")
		if stats.get("profile_fingerprint") != profile.fingerprint:
			raise ValueError("最新扫描配置与当前配置不同；请使用对应配置审计，不混用城市/搜索词范围")
		# Mixed query windows cannot safely be reconstructed from a legacy city
		# cap.  A direct scanner override, corrupt summary, or stale producer must
		# not be rendered as the current profile's 50/3 protection plan.
		if profile.query_page_limits and stats.get("search_page_limits") != profile.search_page_limits:
			raise ValueError("最新扫描的 search_page_limits 与当前混合配置不一致；拒绝将实际采集误标为配置窗口")
		run_ids = resolve_observation_run_ids(connection, latest, stats)
		marks = ",".join("?" for _ in run_ids)
		page_evidence = _natural_end_page_evidence(
			connection, run_ids=run_ids, expected_searches=expected,
			require_visible_state=stats.get("boss_search_transport") == "page_capture",
		)
		# Raw batches can still look terminal after a later stop, a resumed run, or
		# a configured-cap boundary.  Preserve them below, but do not promote that
		# evidence to a verified visible-list end unless the *latest* logical run
		# itself completed the exact configured window without a stop condition.
		completed_searches = stats.get("completed_searches")
		failed_searches = stats.get("failed_searches")
		truncated_searches = stats.get("truncated_searches")
		page_caps = stats.get("page_cap_reached_searches")
		terminal_verification_eligible = (
			latest["status"] == "completed"
			and stats.get("complete") is True
			and isinstance(completed_searches, list)
			and set(completed_searches) == expected
			and isinstance(failed_searches, list)
			and not failed_searches
			and isinstance(truncated_searches, list)
			and not truncated_searches
			and isinstance(page_caps, list)
			and not page_caps
			and not stats.get("stop_code")
			and not stats.get("stop_reason")
		)
		captures = {item["search"]: item for item in page_evidence["searches"]}
		observations: dict[str, dict[str, int]] = {}
		has_observations = _table_exists(connection, "radar_run_observations")
		if has_observations:
			for row in connection.execute(
				f"SELECT shard, query, COUNT(DISTINCT job_pk) AS unique_jobs, MAX(page) AS deepest_batch "
				f"FROM radar_run_observations WHERE run_id IN ({marks}) GROUP BY shard, query", run_ids,
			):
				observations[f"{row['shard']} / {row['query']}"] = {
					"relevant_unique_jobs": int(row["unique_jobs"]),
					"deepest_observed_batch": int(row["deepest_batch"]),
				}
		rows = []
		for city in profile.cities:
			for query in profile.queries:
				key = f"{city.name} / {query}"
				capture = captures.get(key, {"recorded_batches": 0, "raw_rows": None, "batches": []})
				batches = capture["batches"]
				sequence_continuous = _batch_sequence_continuous(batches)
				reasons = []
				if key in stats.get("page_cap_reached_searches", []):
					reasons.append("reached_configured_cap")
				if not batches:
					reasons.append("raw_batch_evidence_missing")
				elif batches[-1]["has_more"]:
					reasons.append("list_has_more")
				elif batches[-1]["page_size"] is None:
					reasons.append("terminal_page_size_unknown")
				elif batches[-1]["raw_rows"] >= batches[-1]["page_size"]:
					reasons.append("full_terminal_batch")
				# ``_natural_end_page_evidence`` deliberately treats a final
				# ``hasMore=True`` as a natural-end failure.  That is correct for
				# terminal certification, but does not mean batches 1..N are
				# discontinuous inside a configured observation window.
				if not sequence_continuous:
					reasons.append("batch_sequence_unverified")
				if key in page_evidence["missing_visible_state"]:
					reasons.append("same_page_state_unverified")
				if any(item["search"] == key for item in page_evidence["page_rollbacks"]):
					reasons.append("later_segment_restarted_search")
				rows.append({
					"city": city.name, "query": query,
					"configured_page_limit": profile.page_limit_for_city(city, query),
					"recorded_raw_batches": capture["recorded_batches"],
					"raw_rows": capture["raw_rows"],
					**observations.get(key, {"relevant_unique_jobs": 0 if has_observations and batches else None, "deepest_observed_batch": None}),
					"terminal_batch": batches[-1] if batches else None,
					"batch_sequence_continuous": sequence_continuous,
					"uncertainty_reasons": reasons,
					"batches": batches,
				})
	return {
		"profile": profile.name, "run_id": latest["run_id"], "run_ids": list(run_ids),
		"logical_scan_id": stats.get("logical_scan_id") or latest["run_id"],
		"status": latest["status"], "observed_until": latest["completed_at"] or latest["started_at"],
		"platform_requests": 0, "audit_platform_requests": 0, "city_inventory_exhausted": False,
		"request_count_scope": "仅本次离线审计，不是原扫描请求数",
		"terminal_verification_eligible": terminal_verification_eligible,
		"visible_list_end_verified": terminal_verification_eligible and page_evidence["proven"],
		"stop_code": stats.get("stop_code") or "", "stop_reason": stats.get("stop_reason") or "",
		"searches": rows,
		"note": "原始读取条数只取逐批采集证据；缺失显示未知，不用去重后岗位数或总量平均分摊补造。页面终点不证明全城搜全；首次发现不等于发布时间。",
	}


def _batch_sequence_continuous(batches: list[dict[str, Any]]) -> bool:
	"""Check observed batch continuity without requiring a natural terminal page."""
	if not batches:
		return False
	try:
		pages = [int(item["batch"]) for item in batches]
		prior_has_more = [bool(item["has_more"]) for item in batches[:-1]]
	except (KeyError, TypeError, ValueError):
		return False
	return pages == list(range(1, pages[-1] + 1)) and all(prior_has_more)


def write_coverage_report(report: dict[str, Any], output_path: Path) -> dict[str, str]:
	"""Write explicit JSON and human-readable Markdown reports, never source data."""
	if output_path.suffix.lower() != ".json":
		raise ValueError("覆盖报告输出必须使用 .json 文件名")
	markdown_path = output_path.with_suffix(".md")
	if output_path.exists() or markdown_path.exists():
		raise ValueError("报告目标已存在，已拒绝覆盖；请使用新的报告文件名")
	output_path.parent.mkdir(parents=True, exist_ok=True)
	lines = [
		"# 岗位雷达覆盖证据", "", f"配置：{report['profile']}；数据截至：{report['observed_until']}",
		"", report["note"], "", "| 城市 / 搜索词 | 有证据原始条数 | 去重后相关岗位 | 相关岗位最深批次 | 配置保护上限 | 终批证据 |",
		"| --- | ---: | ---: | ---: | ---: | --- |",
	]
	for row in report["searches"]:
		terminal = row["terminal_batch"]
		end = "未留存" if terminal is None else f"第{terminal['batch']}批，{terminal['raw_rows']}条，hasMore={terminal['has_more']}"
		label = f"{row['city']} / {row['query']}".replace("|", "\\|").replace("\n", " ")
		raw = row["raw_rows"] if row["raw_rows"] is not None else "未知"
		relevant = row["relevant_unique_jobs"] if row["relevant_unique_jobs"] is not None else "未知"
		lines.append(
			f"| {label} | {raw} | {relevant} | {row['deepest_observed_batch'] or '—'} | "
			f"{row['configured_page_limit']} | {end} |"
		)
	lines += ["", "逐批原始数量及待核原因见同名 JSON。相关岗位数不能用于反推原始列表是否满载。", ""]
	with output_path.open("x", encoding="utf-8") as handle:
		handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
	with markdown_path.open("x", encoding="utf-8") as handle:
		handle.write("\n".join(lines))
	return {"json": str(output_path.resolve()), "markdown": str(markdown_path.resolve())}
