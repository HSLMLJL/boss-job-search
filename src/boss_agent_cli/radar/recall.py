"""Offline comparison of one main scan and a supplemental query probe.

The two profiles intentionally keep separate SQLite files.  This module joins
only the explicitly named logical scans, never the databases' historical job
inventories, so a stale observation cannot hide a present-day recall gap.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from boss_agent_cli.radar.evidence_scope import resolve_observation_run_ids
from boss_agent_cli.radar.profile import RadarProfile


_CHINA_TZ = timezone(timedelta(hours=8))
_SUPPLEMENT_QUERIES = frozenset(
	{
		"人工智能产品经理",
		"大模型产品经理",
		"AIGC产品经理",
		"智能体产品经理",
		"Agent产品经理",
	}
)


class RecallComparisonError(ValueError):
	"""Raised when the requested scan pair is not a valid comparison window."""


def build_recall_report(
	*,
	main_db_path: Path,
	supplement_db_path: Path,
	main_logical_scan_id: str,
	supplement_logical_scan_id: str,
	city: str = "北京",
	primary_query: str = "AI产品经理",
	comparison_window_hours: float | None = None,
	main_profile: RadarProfile | None = None,
	supplement_profile: RadarProfile | None = None,
) -> dict[str, Any]:
	"""Compare explicitly scoped main and supplemental query observations.

	Without profiles this retains the original, deliberately narrow six-city / five
	Beijing-term compatibility gate.  Passing *both* profiles opts into the
	general path: each selected logical run must exactly complete its configured
	city × query set and its immutable observation-run chain is verified.
	"""
	if (main_profile is None) != (supplement_profile is None):
		raise RecallComparisonError("通用对比必须同时提供主扫描和补充扫描 profile")
	configured = main_profile is not None
	with ExitStack() as stack:
		main_connection = stack.enter_context(closing(_open_read_only(main_db_path)))
		supplement_connection = stack.enter_context(closing(_open_read_only(supplement_db_path)))
		main_scan = _load_scan(main_connection, main_logical_scan_id, label="主扫描", strict_scope=configured)
		supplement_scan = _load_scan(supplement_connection, supplement_logical_scan_id, label="补充扫描", strict_scope=configured)
		if configured:
			assert main_profile is not None and supplement_profile is not None
			main_completed_stats = _validate_profile_scan(main_scan, main_profile, city=city, label="主扫描")
			supplement_completed_stats = _validate_profile_scan(
				supplement_scan, supplement_profile, city=city, label="补充扫描",
				allow_configured_window=True,
			)
			main_queries = main_profile.queries
			supplement_queries = supplement_profile.queries
			if set(main_queries) & set(supplement_queries):
				raise RecallComparisonError("通用对比的主扫描与补充扫描查询词不得重叠")
		else:
			main_completed_stats = _validate_main_scan(main_scan, city=city, primary_query=primary_query)
			supplement_completed_stats = _validate_supplement_scan(
				supplement_scan, city=city, primary_query=primary_query,
			)
			main_queries = (primary_query,)
			supplement_queries = _completed_queries(supplement_completed_stats, city=city)
		time_scope = _comparison_time_scope(main_scan, supplement_scan, comparison_window_hours)

		main_jobs = _load_jobs(
			main_connection,
			main_scan["run_ids"],
			city=city,
			queries=main_queries,
		)
		supplement_jobs = _load_jobs(
			supplement_connection,
			supplement_scan["run_ids"],
			city=city,
			queries=supplement_queries,
		)
		main_by_key = {job["match_key"]: job for job in main_jobs}
		supplement_by_key = {job["match_key"]: job for job in supplement_jobs}
		overlap_keys = sorted(set(main_by_key) & set(supplement_by_key))
		supplement_only_keys = sorted(set(supplement_by_key) - set(main_by_key))

		overlap_jobs = [_comparison_job(supplement_by_key[key], main_seen=True) for key in overlap_keys]
		supplement_only_jobs = [_comparison_job(supplement_by_key[key], main_seen=False) for key in supplement_only_keys]
		by_query = {
			query: sum(query in job["supplement_queries"] for job in supplement_only_jobs)
			for query in supplement_queries
		}
		query_contributions = _query_contributions(
			supplement_jobs=supplement_jobs,
			main_keys=set(main_by_key),
			supplement_queries=supplement_queries,
		)
		counts = {
			"main_primary_jobs": len(main_jobs),
			"supplement_unique_jobs": len(supplement_jobs),
			"overlap_jobs": len(overlap_jobs),
			"supplement_only_jobs": len(supplement_only_jobs),
			"supplement_query_contributions": query_contributions,
			# Retained for backwards compatibility.  Its values can overlap and
			# must not be used to decide that a query is redundant.
			"supplement_only_by_query": by_query,
		}
		if not configured:
			# The legacy JSON contract has no generic main count.  Keep it byte-for-
			# byte compatible for consumers that compare the complete object.
			counts.pop("main_primary_jobs")
			counts["main_beijing_primary_jobs"] = len(main_jobs)
		return {
			**time_scope,
			"comparison_scope": {
				"report_kind": "exploratory_supplement_query_contribution",
				"valid_for_recall": False,
				"recall_validity_reason": (
					"主词与补词来自同一平台且采集有时间先后，不是独立标注样本；"
					"本报告不计算或声称正式召回率。"
				),
				"city": city,
				"primary_query": primary_query if not configured else None,
				"main_queries": list(main_queries),
				"supplement_queries": list(supplement_queries),
				"match_rule": "same platform + stable platform_job_id; identity_key only when an ID is absent",
				"main_set": "岗位仅来自指定主扫描逻辑会话、指定城市和主词观察记录，不等同于平台主词全集",
				"supplement_set": "岗位仅来自指定补充扫描逻辑会话、指定城市和补充词观察记录",
				"main_primary_coverage": (
					_queries_coverage_scope(main_completed_stats, city=city, queries=main_queries, profile=main_profile)
					if configured
					else _main_primary_coverage_scope(
						main_completed_stats, city=city, primary_query=primary_query,
					)
				),
				"supplement_coverage": _queries_coverage_scope(
					supplement_completed_stats, city=city, queries=supplement_queries,
					profile=supplement_profile if configured else None,
				),
				"limit": "补充词的差集只证明在已观察窗口内额外命中，不代表平台全量、主词全集或正式召回率",
			},
			"main_scan": _scan_summary(main_scan),
			"supplement_scan": _scan_summary(supplement_scan),
			"counts": counts,
			"supplement_only_jobs": supplement_only_jobs,
			"overlap_jobs": overlap_jobs,
		}


def write_recall_report(report: dict[str, Any], output_path: Path) -> Path:
	"""Write a report without touching either source database."""
	output_path.parent.mkdir(parents=True, exist_ok=True)
	if output_path.exists():
		raise RecallComparisonError(f"召回报告输出已存在，拒绝覆盖：{output_path}")
	with output_path.open("x", encoding="utf-8") as handle:
		handle.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
	return output_path


def _open_read_only(path: Path) -> sqlite3.Connection:
	path = path.expanduser().resolve()
	if not path.is_file():
		raise RecallComparisonError(f"雷达数据库不存在：{path}")
	connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA query_only = ON")
	return connection


def _load_scan(
	connection: sqlite3.Connection,
	logical_scan_id: str,
	*,
	label: str,
	strict_scope: bool = False,
) -> dict[str, Any]:
	if not logical_scan_id.strip():
		raise RecallComparisonError(f"{label}必须提供 logical_scan_id")
	rows = connection.execute(
		"SELECT run_id, mode, status, started_at, completed_at, stats_json FROM radar_runs "
		"WHERE status IN ('completed', 'stopped', 'failed') ORDER BY started_at"
	).fetchall()
	runs: list[dict[str, Any]] = []
	for run_id, mode, status, started_at, completed_at, raw_stats in rows:
		try:
			stats = json.loads(str(raw_stats))
		except (TypeError, ValueError):
			continue
		if not isinstance(stats, dict):
			continue
		candidate = str(stats.get("logical_scan_id") or run_id)
		if candidate == logical_scan_id:
			runs.append(
				{
					"run_id": str(run_id),
					"mode": str(mode),
					"status": str(status),
					"started_at": str(started_at),
					"completed_at": str(completed_at or ""),
					"stats": stats,
				}
			)
	if not runs:
		raise RecallComparisonError(f"{label}找不到逻辑扫描：{logical_scan_id}")
	if strict_scope:
		# A logical ID is a user-visible label, not sufficient authority to merge
		# rows.  Resolve the final row's declared physical chain against immutable
		# run metadata before it is used as report evidence.
		latest = runs[-1]
		try:
			run_ids = resolve_observation_run_ids(connection, latest, latest["stats"])
		except ValueError as exc:
			raise RecallComparisonError(f"{label}逻辑运行链无效：{exc}") from exc
		by_id = {run["run_id"]: run for run in runs}
		try:
			chain = [by_id[run_id] for run_id in run_ids]
		except KeyError as exc:
			raise RecallComparisonError(f"{label}逻辑运行链包含不属于该 logical_scan_id 的分段") from exc
		return {"logical_scan_id": logical_scan_id, "run_ids": list(run_ids), "runs": chain}
	return {"logical_scan_id": logical_scan_id, "run_ids": [run["run_id"] for run in runs], "runs": runs}


def _final_completed_run(scan: dict[str, Any], *, label: str) -> dict[str, Any]:
	completed = [run for run in scan["runs"] if run["status"] == "completed"]
	if not completed:
		raise RecallComparisonError(f"{label}尚未完成，不能进行召回对比")
	final = completed[-1]
	if not bool(final["stats"].get("complete")):
		raise RecallComparisonError(f"{label}最终运行未标记 complete，不能进行召回对比")
	return final


def _validate_main_scan(scan: dict[str, Any], *, city: str, primary_query: str) -> dict[str, Any]:
	final = _final_completed_run(scan, label="主扫描")
	stats = final["stats"]
	if stats.get("page_cap_reached_searches"):
		raise RecallComparisonError("主扫描触及分页上限，未自然结束，不能作为主词召回基准")
	if stats.get("stop_code"):
		raise RecallComparisonError(f"主扫描有停止代码：{stats['stop_code']}")
	completed = _string_list(stats.get("completed_searches"))
	main_cities = {key.rsplit(" / ", 1)[0] for key in completed if key.endswith(f" / {primary_query}")}
	if len(main_cities) != 6:
		raise RecallComparisonError(
			f"主扫描必须完成六个城市的 {primary_query}，当前为 {len(main_cities)} 个：{'、'.join(sorted(main_cities))}"
		)
	if city not in main_cities:
		raise RecallComparisonError(f"主扫描六城范围不含对比城市 {city}")
	return stats


def _validate_supplement_scan(scan: dict[str, Any], *, city: str, primary_query: str) -> dict[str, Any]:
	final = _final_completed_run(scan, label="补充扫描")
	stats = final["stats"]
	if stats.get("stop_code"):
		raise RecallComparisonError(f"补充扫描有停止代码：{stats['stop_code']}")
	limits = stats.get("city_page_limits")
	if not isinstance(limits, dict) or set(limits.values()) != {3}:
		raise RecallComparisonError("补充扫描必须使用每城市 3 页的观察窗口")
	queries = _completed_queries(stats, city=city)
	if set(queries) != _SUPPLEMENT_QUERIES or primary_query in queries:
		raise RecallComparisonError(
			f"补充扫描必须只完成{city}的五个约定补充词，当前为：{'、'.join(queries) or '无'}"
		)
	return stats


def _validate_profile_scan(
	scan: dict[str, Any], profile: RadarProfile, *, city: str, label: str,
	allow_configured_window: bool = False,
) -> dict[str, Any]:
	"""Validate an explicit configured comparison source.

	This is deliberately stricter than the legacy fixed-window route: all
	configured searches must have completed, the requested city must belong to
	the configuration, and every physical observation segment has already been
	validated by :func:`resolve_observation_run_ids` in ``_load_scan``.
	"""
	if city not in {item.name for item in profile.cities}:
		raise RecallComparisonError(f"{label} profile 不含对比城市 {city}")
	if not scan["runs"] or scan["runs"][-1]["status"] != "completed":
		raise RecallComparisonError(f"{label}最终运行未完成，不能进行配置对比")
	final = _final_completed_run(scan, label=label)
	stats = final["stats"]
	if stats.get("profile_fingerprint") != profile.fingerprint:
		raise RecallComparisonError(f"{label} profile_fingerprint 与指定 profile 不一致")
	if profile.query_page_limits and stats.get("search_page_limits") != profile.search_page_limits:
		raise RecallComparisonError(f"{label} search_page_limits 与指定 profile 不一致")
	expected = {f"{item.name} / {query}" for item in profile.cities for query in profile.queries}
	# New mixed-depth profiles use these fields to distinguish a deliberate
	# three-page probe from a natural page end.  Do not reinterpret an omitted or
	# malformed field as an empty list; old fixed-window reports retain their
	# historical compatibility path below.
	if profile.query_page_limits:
		for field in ("failed_searches", "truncated_searches", "page_cap_reached_searches"):
			value = stats.get(field)
			if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
				raise RecallComparisonError(f"{label}缺少或包含非法的 {field}，不能进行混合配置对比")
	completed = set(_string_list(stats.get("completed_searches")))
	if completed != expected:
		missing = sorted(expected - completed)
		extra = sorted(completed - expected)
		detail = []
		if missing:
			detail.append(f"缺少：{'、'.join(missing)}")
		if extra:
			detail.append(f"超出配置：{'、'.join(extra)}")
		raise RecallComparisonError(f"{label}未完整覆盖指定 profile 的城市×词集合（{'；'.join(detail)}）")
	for field in ("stop_code", "stop_reason", "failed_searches", "truncated_searches"):
		value = stats.get(field)
		if value not in (None, "", [], {}):
			raise RecallComparisonError(f"{label}存在 {field}，不能进行配置对比")
	page_caps = set(_string_list(stats.get("page_cap_reached_searches")))
	if page_caps and not allow_configured_window:
		raise RecallComparisonError(f"{label}触及分页上限，未自然结束，不能作为配置对比来源")
	if page_caps and not page_caps <= completed:
		raise RecallComparisonError(f"{label}分页窗口包含未完成搜索：{'、'.join(sorted(page_caps - completed))}")
	if page_caps and not page_caps <= expected:
		raise RecallComparisonError(f"{label}分页窗口超出指定 profile：{'、'.join(sorted(page_caps - expected))}")
	return stats


def _completed_queries(stats: dict[str, Any], *, city: str) -> tuple[str, ...]:
	prefix = f"{city} / "
	queries = [key[len(prefix) :] for key in _string_list(stats.get("completed_searches")) if key.startswith(prefix)]
	return tuple(dict.fromkeys(queries))


def _main_primary_coverage_scope(
	stats: dict[str, Any], *, city: str, primary_query: str, configured_page_limit: int | None = None,
) -> dict[str, Any]:
	"""Describe the evidence boundary without treating absence as exhaustion.

	Older scan records predate capture-boundary fields.  They remain useful for
	exploratory query contribution, but their missing fields must not silently
	be read as a successful terminal page.
	"""
	search = f"{city} / {primary_query}"
	page_caps = set(_string_list(stats.get("page_cap_reached_searches")))
	terminal_full = set(_string_list(stats.get("terminal_full_searches")))
	terminal_missing = set(_string_list(stats.get("terminal_evidence_missing_searches")))
	evidence_fields_recorded = (
		"terminal_full_searches" in stats and "terminal_evidence_missing_searches" in stats
	)
	if search in page_caps:
		boundary = "page_cap_reached"
	elif search in terminal_full:
		boundary = "terminal_page_full"
	elif search in terminal_missing:
		boundary = "terminal_evidence_missing"
	elif not evidence_fields_recorded:
		boundary = "terminal_evidence_unrecorded"
	else:
		boundary = "reported_terminal"
	return {
		"search": search,
		"configured_page_limit": configured_page_limit,
		"boundary_status": boundary,
		"terminal_evidence_recorded": evidence_fields_recorded,
		"page_cap_reached": search in page_caps,
		"terminal_page_full": search in terminal_full,
		"terminal_evidence_missing": search in terminal_missing,
		# A rendered page is an observation boundary, not proof that the platform
		# has no more matching jobs.  Never upgrade it to an exhaustion claim.
		"platform_results_exhausted": False,
		"visible_list_reported": boundary == "reported_terminal",
		"interpretation": (
			"主词集合仅是已观察岗位；即使记录为终页，也因同源搜索比较不能作为正式召回率基准。"
		),
	}


def _queries_coverage_scope(
	stats: dict[str, Any], *, city: str, queries: tuple[str, ...], profile: RadarProfile | None = None,
) -> dict[str, Any]:
	"""Return one transparent boundary record per configured main query."""
	per_query = {
		query: _main_primary_coverage_scope(
			stats, city=city, primary_query=query,
			configured_page_limit=profile.page_limit_for_city(
				next(item for item in profile.cities if item.name == city), query,
			) if profile is not None else None,
		)
		for query in queries
	}
	return {
		"city": city,
		"queries": per_query,
		"all_platform_results_exhausted": False,
		"interpretation": "每个主词均只代表已观察窗口；差集报告始终不能作为正式召回率。",
	}


def _query_contributions(
	*,
	supplement_jobs: list[dict[str, Any]],
	main_keys: set[str],
	supplement_queries: tuple[str, ...],
) -> dict[str, dict[str, int]]:
	"""Measure per-query contribution without double-counting overlap as unique."""
	contributions: dict[str, dict[str, int]] = {}
	for query in supplement_queries:
		matching = [job for job in supplement_jobs if query in job["supplement_queries"]]
		contributions[query] = {
			"total_unique_jobs": len(matching),
			"additional_vs_primary_jobs": sum(job["match_key"] not in main_keys for job in matching),
			"unique_vs_other_supplement_queries_jobs": sum(
				len(job["supplement_queries"]) == 1 for job in matching
			),
			"unique_additional_vs_primary_and_other_supplement_queries_jobs": sum(
				job["match_key"] not in main_keys and len(job["supplement_queries"]) == 1
				for job in matching
			),
		}
	return contributions


def _scan_date(scan: dict[str, Any], *, label: str) -> date:
	final = _final_completed_run(scan, label=label)
	value = final["completed_at"] or final["started_at"]
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_CHINA_TZ).date()
	except (AttributeError, ValueError) as exc:
		raise RecallComparisonError(f"{label}缺少可解析的完成时间") from exc


def _validate_scan_day(scan: dict[str, Any], *, target_date: date, label: str) -> None:
	"""Reject a logical id whose segments were accidentally joined across days."""
	for run in scan["runs"]:
		for field in ("started_at", "completed_at"):
			value = run[field]
			try:
				observed_date = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_CHINA_TZ).date()
			except (AttributeError, ValueError) as exc:
				raise RecallComparisonError(f"{label}分段 {run['run_id']} 缺少可解析的 {field}") from exc
			if observed_date != target_date:
				raise RecallComparisonError(
					f"{label}逻辑扫描跨自然日：分段 {run['run_id']} 的 {field} 为 {observed_date.isoformat()}，"
					f"目标日为 {target_date.isoformat()}"
				)


def _comparison_time_scope(
	main_scan: dict[str, Any], supplement_scan: dict[str, Any], max_hours: float | None,
) -> dict[str, Any]:
	"""Keep calendar-day reports strict; opt into an explicitly bounded window.

	A continuous overnight collection is useful for applications, but must not
	be relabelled as one calendar day's new jobs. Neither mode includes older
	logical scans or changes completeness/identity/newness validation.
	"""
	if max_hours is not None:
		from boss_agent_cli.radar.scan_window import ScanWindowError, validate_rolling_scan_window

		try:
			window = validate_rolling_scan_window(main_scan, supplement_scan, max_hours=max_hours)
		except ScanWindowError as exc:
			raise RecallComparisonError(str(exc)) from exc
		return {"scan_date": None if window["crosses_midnight"] else window["dates"][0], "scan_window": window}
	main_date = _scan_date(main_scan, label="主扫描")
	supplement_date = _scan_date(supplement_scan, label="补充扫描")
	if main_date != supplement_date:
		raise RecallComparisonError(
			f"主扫描与补充扫描不是同一中国自然日：{main_date.isoformat()} vs {supplement_date.isoformat()}"
		)
	_validate_scan_day(main_scan, target_date=main_date, label="主扫描")
	_validate_scan_day(supplement_scan, target_date=main_date, label="补充扫描")
	return {"scan_date": main_date.isoformat(), "scan_window": None}


def _load_jobs(
	connection: sqlite3.Connection,
	run_ids: Iterable[str],
	*,
	city: str,
	queries: tuple[str, ...],
) -> list[dict[str, Any]]:
	runs = tuple(run_ids)
	if not runs or not queries:
		return []
	run_marks = ",".join("?" for _ in runs)
	query_marks = ",".join("?" for _ in queries)
	rows = connection.execute(
		f"""
		SELECT j.platform, j.identity_key, j.platform_job_id, j.title, j.company,
			j.city, j.district, j.salary, j.experience, j.education, j.first_seen_at,
			o.query
		FROM radar_run_observations AS o
		JOIN radar_jobs AS j ON j.id = o.job_pk
		WHERE o.run_id IN ({run_marks}) AND o.shard = ? AND o.query IN ({query_marks})
		GROUP BY j.platform, j.identity_key, j.platform_job_id, j.title, j.company,
			j.city, j.district, j.salary, j.experience, j.education, j.first_seen_at, o.query
		ORDER BY j.first_seen_at DESC, j.platform_job_id, j.identity_key
		""",
		(*runs, city, *queries),
	).fetchall()
	jobs: dict[str, dict[str, Any]] = {}
	for row in rows:
		platform, identity_key, platform_job_id = (str(row[0]), str(row[1]), str(row[2]))
		match_key = _match_key(platform, platform_job_id, identity_key)
		job = jobs.setdefault(
			match_key,
			{
				"match_key": match_key,
				"platform": platform,
				"platform_job_id": platform_job_id,
				"identity_key": identity_key,
				"title": str(row[3]),
				"company": str(row[4]),
				"city": str(row[5]),
				"district": str(row[6]),
				"salary": str(row[7]),
				"experience": str(row[8]),
				"education": str(row[9]),
				"first_discovered_at": str(row[10]),
				"supplement_queries": [],
			},
		)
		query = str(row[11])
		if query not in job["supplement_queries"]:
			job["supplement_queries"].append(query)
	return list(jobs.values())


def _match_key(platform: str, platform_job_id: str, identity_key: str) -> str:
	return f"id:{platform}:{platform_job_id}" if platform_job_id else f"identity:{platform}:{identity_key}"


def _comparison_job(job: dict[str, Any], *, main_seen: bool) -> dict[str, Any]:
	job_id = str(job["platform_job_id"])
	return {
		"match_key": job["match_key"],
		"platform": job["platform"],
		"platform_job_id": job_id,
		"title": job["title"],
		"company": job["company"],
		"city": job["city"],
		"district": job["district"],
		"salary": job["salary"],
		"experience": job["experience"],
		"education": job["education"],
		"first_discovered_at": job["first_discovered_at"],
		"supplement_queries": sorted(job["supplement_queries"]),
		"present_in_main_scan": main_seen,
		"url": f"https://www.zhipin.com/job_detail/{quote(job_id, safe='')}.html" if job_id else "",
	}


def _scan_summary(scan: dict[str, Any]) -> dict[str, Any]:
	final = _final_completed_run(scan, label="扫描")
	return {
		"logical_scan_id": scan["logical_scan_id"],
		"run_ids": scan["run_ids"],
		"completed_at": final["completed_at"],
		"completed_searches": _string_list(final["stats"].get("completed_searches")),
		"page_cap_reached_searches": _string_list(final["stats"].get("page_cap_reached_searches")),
	}


def _string_list(value: Any) -> list[str]:
	return [str(item) for item in value] if isinstance(value, list) and all(isinstance(item, str) for item in value) else []
