"""Read-only preparation of a single, auditable radar delivery bundle.

This deliberately sits between scan storage and a future workbook renderer.  It
does not discover jobs or decide that a job is globally new: it only combines
the two explicitly named, validated logical scan windows.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from boss_agent_cli.api.models import employment_type_from_raw
from boss_agent_cli.radar.recall import (
	RecallComparisonError,
	_comparison_time_scope,
	_completed_queries,
	_load_scan,
	_match_key,
	_open_read_only,
	_scan_summary,
	_validate_profile_scan,
	_validate_main_scan,
	_validate_supplement_scan,
)
from boss_agent_cli.radar.profile import RadarProfile
from boss_agent_cli.radar.history_anchor import HistoryAnchorError, validate_history_anchor_annotations


class DeliveryBundleError(ValueError):
	"""Raised when scan evidence cannot safely be made into a delivery bundle."""


_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_~-]+$")
_CANONICAL_JOB_PATH = re.compile(r"^/job_detail/[A-Za-z0-9_~-]+\.html$")
_AI_DETAIL_FIELDS = {"title", "skills", "job_labels", "description"}
_AI_DETAIL_CONTEXT_LIMIT = 120
_AI_DETAIL_MAXIMUM = 5
_AI_DETAIL_SECRET_PATTERN = re.compile(
	r"(?:\b(?:api[ _-]?key|access[ _-]?token|secret|password|authorization|bearer|cookie|security[ _-]?id|encrypt[ _-]?job[ _-]?id)\b"
	r"|\btoken\s*[:=]"
	r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d))",
	re.IGNORECASE,
)
_PRESENCE_LABELS = {
	"main_only": "仅主扫发现",
	"supplement_only": "仅补扫发现",
	"main_and_supplement": "主扫与补扫均发现",
}


def build_delivery_bundle(
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
	"""Merge explicitly selected, validated main and supplemental scan windows.

	Pass both profiles for a configuration-driven comparison.  Omitting both
	preserves the established six-city main + Beijing five-term legacy contract.
	In either mode, neither database's historical job inventory is considered.
	"""
	if (main_profile is None) != (supplement_profile is None):
		raise DeliveryBundleError("main_profile 与 supplement_profile 必须同时提供，或同时省略以使用旧六城规则")
	profile_driven = main_profile is not None
	if profile_driven:
		assert main_profile is not None and supplement_profile is not None
		if set(main_profile.queries) & set(supplement_profile.queries):
			raise DeliveryBundleError("通用对比的主扫描与补充扫描查询词不得重叠")
	try:
		with ExitStack() as stack:
			main_connection = stack.enter_context(closing(_open_read_only(main_db_path)))
			supplement_connection = stack.enter_context(closing(_open_read_only(supplement_db_path)))
			main_scan = _load_scan(
				main_connection, main_logical_scan_id, label="主扫描", strict_scope=profile_driven
			)
			supplement_scan = _load_scan(
				supplement_connection, supplement_logical_scan_id, label="补充扫描", strict_scope=profile_driven
			)
			main_queries: tuple[str, ...]
			if main_profile is None or supplement_profile is None:
				main_stats = _validate_main_scan(main_scan, city=city, primary_query=primary_query)
				supplement_stats = _validate_supplement_scan(
					supplement_scan, city=city, primary_query=primary_query
				)
				main_cities = _cities_for_query(main_stats, primary_query)
				main_queries = (primary_query,)
				supplement_queries = _completed_queries(supplement_stats, city=city)
			else:
				main_stats = _validate_profile_scan(main_scan, main_profile, city=city, label="主扫描")
				supplement_stats = _validate_profile_scan(
					supplement_scan,
					supplement_profile,
					city=city,
					label="补充扫描",
					allow_configured_window=True,
				)
				main_cities = tuple(item.name for item in main_profile.cities)
				main_queries = tuple(main_profile.queries)
				supplement_queries = tuple(supplement_profile.queries)
			time_scope = _comparison_time_scope(main_scan, supplement_scan, comparison_window_hours)

			main_rows = _load_observations(
				main_connection,
				main_scan["run_ids"],
				cities=main_cities,
				queries=main_queries,
				source="main",
			)
			supplement_rows = _load_observations(
				supplement_connection,
				supplement_scan["run_ids"],
				cities=(city,),
				queries=supplement_queries,
				source="supplement",
			)
	except RecallComparisonError as exc:
		raise DeliveryBundleError(str(exc)) from exc

	jobs = _merge_jobs(main_rows, supplement_rows)
	supplement_limits = {
		query: supplement_profile.search_page_limits[f"{city} / {query}"] if supplement_profile else 3
		for query in supplement_queries
	}
	uniform_supplement_limit = next(iter(set(supplement_limits.values()))) if len(set(supplement_limits.values())) == 1 else None
	return {
		"schema_version": 1,
		**time_scope,
		"coverage": {
			"main": {
				**_scan_summary(main_scan),
				"cities": sorted(main_cities),
				"query": primary_query if not profile_driven else None,
				"queries": list(main_queries),
				"profile_driven": profile_driven,
				"coverage_label": _coverage_label(
					main_stats, cities=main_cities, queries=main_queries, role="主扫"
				),
			},
			"supplement": {
				**_scan_summary(supplement_scan),
				"city": city,
				"cities": [city],
				"queries": list(supplement_queries),
				"profile_driven": profile_driven,
				"page_window_per_query": uniform_supplement_limit,
				"search_page_limits": {f"{city} / {query}": pages for query, pages in supplement_limits.items()},
				"coverage_label": _coverage_label(
					supplement_stats, cities=(city,), queries=supplement_queries, role="补扫交付子范围"
				),
			},
		},
		"merge_policy": {
			"identity": "同平台 + stable platform_job_id；缺少 ID 时才使用 identity_key",
			"no_fuzzy_deduplication": "公司和标题相似、但岗位 ID 不同的记录不会被删除或合并",
			"global_newness": "补充库的首次发现仅是补充配置本地历史，绝不作为全局新增声明",
			"repost": "仅标记数据库中当前选定运行已有的 reposted 事件证据",
		},
		"counts": {
			"unique_jobs": len(jobs),
			"main_only_jobs": sum(job["presence"] == "main_only" for job in jobs),
			"supplement_only_jobs": sum(job["presence"] == "supplement_only" for job in jobs),
			"overlap_jobs": sum(job["presence"] == "main_and_supplement" for job in jobs),
		},
		"jobs": jobs,
	}


def write_delivery_bundle(bundle: dict[str, Any], *, json_path: Path, html_path: Path) -> tuple[Path, Path]:
	"""Write the prepared data and a manual-click-only application entry page."""
	json_path = json_path.resolve()
	html_path = html_path.resolve()
	if json_path == html_path:
		raise DeliveryBundleError("JSON 与 HTML 输出路径不能相同")
	if json_path.exists() or html_path.exists():
		raise DeliveryBundleError("交付包输出已存在，已拒绝覆盖历史文件")
	json_path.parent.mkdir(parents=True, exist_ok=True)
	html_path.parent.mkdir(parents=True, exist_ok=True)
	json_payload = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
	html_payload = render_delivery_html(bundle)
	json_created = False
	html_created = False
	try:
		with json_path.open("x", encoding="utf-8") as handle:
			json_created = True
			handle.write(json_payload)
		with html_path.open("x", encoding="utf-8") as handle:
			html_created = True
			handle.write(html_payload)
	except OSError as exc:
		# Only remove files opened successfully by this invocation; never replace
		# a user's existing target if a concurrent writer won the race.
		if json_created:
			json_path.unlink()
		if html_created:
			html_path.unlink()
		raise DeliveryBundleError(f"无法写入交付包：{exc}") from exc
	return json_path, html_path


def _coverage_label(
	stats: dict[str, Any], *, cities: Iterable[str], queries: Iterable[str], role: str,
) -> str:
	"""Describe the exact configured range without claiming city-wide exhaustion."""
	city_values = tuple(cities)
	query_values = tuple(queries)
	scope = {f"{city} / {query}" for city in city_values for query in query_values}
	def scoped(name: str) -> list[str]:
		return [item for item in _string_list(stats.get(name)) if item in scope]
	boundary_notes: list[str] = []
	full_terminal = scoped("terminal_full_searches")
	missing_terminal = scoped("terminal_evidence_missing_searches")
	page_caps = scoped("page_cap_reached_searches")
	if full_terminal:
		boundary_notes.append(f"{len(full_terminal)} 个终批满载，页面终点待核")
	if missing_terminal:
		boundary_notes.append(f"{len(missing_terminal)} 个搜索缺少终点证据")
	if page_caps:
		boundary_notes.append(f"{len(page_caps)} 个搜索达到配置页数上限")
	terminal_fields_recorded = all(
		name in stats and isinstance(stats.get(name), list)
		for name in ("terminal_full_searches", "terminal_evidence_missing_searches")
	)
	if not terminal_fields_recorded:
		boundary_notes.append("终点边界字段未留存，页面终点待核")
	elif not boundary_notes:
		boundary_notes.append("已记录当前列表的终点证据")
	return (
		f"{role}配置范围：{len(city_values)} 城、{len(query_values)} 个搜索词；"
		+ "；".join(boundary_notes)
		+ "。页面停止不代表全城岗位已搜全。"
	)


def _string_list(value: Any) -> list[str]:
	return [str(item) for item in value] if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def render_delivery_html(bundle: dict[str, Any]) -> str:
	"""Render links as inert, user-clicked anchors; never embed raw database HTML."""
	coverage = bundle["coverage"]
	main = coverage["main"]
	supplement = coverage["supplement"]
	window = bundle.get("scan_window")
	if isinstance(window, dict):
		time_label = f"采集时间段（北京时间）：{window.get('start_at')} 至 {window.get('end_at')}"
		time_note = "滚动采集窗口，不代表单一自然日的新增；岗位来源差异也可能受采集时间先后影响。"
	else:
		time_label = f"扫描日期：{bundle.get('scan_date')}"
		time_note = "本表为扫描到的岗位清单，不等于今日新增岗位。"
	anchored = isinstance(bundle.get("history_anchor"), dict)
	if anchored:
		try:
			validate_history_anchor_annotations(bundle)
		except HistoryAnchorError as exc:
			raise DeliveryBundleError(f"历史库比对注释无效：{exc}") from exc
	history_labels = {
		"known_old": "此前记录同ID", "first_observed_today": "当日首次记录",
		"absent_from_anchor_at_cutoff": "历史库未记录",
	}
	history_order = {"absent_from_anchor_at_cutoff": 0, "first_observed_today": 1, "known_old": 2}
	jobs = sorted(bundle["jobs"], key=lambda job: history_order.get(job.get("anchor_history_status"), 3)) if anchored else bundle["jobs"]
	rows = []
	for job in jobs:
		candidate_url = str(job.get("boss_url") or "")
		url = candidate_url if _is_canonical_boss_url(candidate_url) else ""
		links = "—"
		if url:
			links = (
				f'<a href="{html.escape(url, quote=True)}">打开 BOSS 岗位</a> '
				f'<a href="{html.escape(_edge_url(url), quote=True)}">在 Edge 打开</a>'
			)
		history_cell = (
			f"<td>{_escape_text(history_labels.get(job.get('anchor_history_status'), '历史比对不确定'))}</td>"
			if anchored else ""
		)
		rows.append(
			"<tr>"
			f"{history_cell}"
			f"<td>{_escape_text(job.get('title'))}</td>"
			f"<td>{_escape_text(job.get('company'))}</td>"
			f"<td>{_escape_text(job.get('city'))}{_district_suffix(job.get('district'))}</td>"
			f"<td>{_escape_text(job.get('salary'))}</td>"
			f"<td>{_escape_text('、'.join(_ai_evidence_values(job.get('ai_evidence'))))}</td>"
			f"<td>{_escape_text(_presence_label(job.get('presence')))}</td>"
			f"<td>{_escape_text(job.get('first_discovered_at'))}</td>"
			f"<td>{_escape_text(job.get('latest_observed_at'))}</td>"
			f"<td>{links}</td>"
			"</tr>"
		)
	return "\n".join(
		[
			"<!doctype html>",
			'<html lang="zh-CN"><head><meta charset="utf-8">',
			"<title>AI 产品经理岗位雷达：人工投递入口</title>",
			"<style>body{font-family:system-ui,sans-serif;margin:24px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:8px;text-align:left;vertical-align:top}th{background:#f3f3f3}a{white-space:nowrap}</style>",
			"</head><body>",
			"<h1>AI 产品经理岗位雷达：人工投递入口</h1>",
			f"<p>{_escape_text(time_label)}。此页不会自动打开链接或发起任何平台请求。</p>",
			f"<p>{_escape_text(time_note)}</p>",
			f"<p>主扫：{_escape_text(main.get('coverage_label'))}。城市：{_escape_text('、'.join(main.get('cities', [])))}</p>",
			f"<p>补扫：{_escape_text(supplement.get('coverage_label'))}。关键词：{_escape_text('、'.join(supplement.get('queries', [])))}</p>",
			"<p>“补充独有”只表示本次主扫描窗口未见；补充库本地首次发现不等于全局新增。</p>",
			(f"<p>已按固定历史库比对分组，截至 {_escape_text(bundle['history_anchor'].get('effective_cutoff_at'))}。"
			"“当日”指截止时刻的北京时间日期，不是打开本表当天。未记录或当日首次记录均不等于平台新发布，已记录同ID也不表示已经投递。"
			"相关性使用库内当前内容，不是历史逐次快照。</p>") if anchored else "",
			"<table><thead><tr>" + ("<th>历史库比对</th>" if anchored else "")
			+ "<th>岗位</th><th>公司</th><th>地区</th><th>薪资</th><th>AI 相关证据</th><th>来源</th><th>来源库首次发现</th><th>最近观察</th><th>人工打开</th></tr></thead><tbody>",
			*rows,
			"</tbody></table></body></html>",
		]
	)


def _cities_for_query(stats: dict[str, Any], query: str) -> tuple[str, ...]:
	prefix = " / "
	return tuple(
		key.rsplit(prefix, 1)[0]
		for key in stats.get("completed_searches", [])
		if isinstance(key, str) and key.endswith(f"{prefix}{query}")
	)


def _load_observations(
	connection: sqlite3.Connection,
	run_ids: Iterable[str],
	*,
	cities: Iterable[str],
	queries: Iterable[str],
	source: str,
	source_id: str | None = None,
	source_label: str | None = None,
	source_database: str | None = None,
) -> list[dict[str, Any]]:
	runs, city_values, query_values = tuple(run_ids), tuple(cities), tuple(queries)
	if not runs or not city_values or not query_values:
		return []
	source_id_value = source_id.strip() if isinstance(source_id, str) and source_id.strip() else source
	source_label_value = (
		source_label.strip() if isinstance(source_label, str) and source_label.strip() else source_id_value
	)
	source_database_value = source_database.strip() if isinstance(source_database, str) and source_database.strip() else ""
	def marks(values: Iterable[str]) -> str:
		return ",".join("?" for _ in values)
	rows = connection.execute(
		f"""
		SELECT j.id, j.platform, j.identity_key, j.platform_job_id, j.title, j.company,
			j.city, j.district, j.salary, j.experience, j.education, j.first_seen_at,
			o.query, o.shard, o.page, o.rank, o.observed_at,
			EXISTS(SELECT 1 FROM radar_events e WHERE e.run_id = o.run_id
				AND e.job_pk = o.job_pk AND e.event_type = 'reposted') AS is_reposted
			, EXISTS(SELECT 1 FROM radar_events e WHERE e.run_id = o.run_id
			AND e.job_pk = o.job_pk AND e.event_type = 'new') AS is_local_new
			, j.payload_json, o.run_id
			, (SELECT e.previous_job_id FROM radar_events e WHERE e.run_id = o.run_id
				AND e.job_pk = o.job_pk AND e.event_type = 'reposted'
				AND e.previous_job_id <> '' ORDER BY e.id DESC LIMIT 1) AS repost_previous_job_id
		FROM radar_run_observations o
		JOIN radar_jobs j ON j.id = o.job_pk
		WHERE o.run_id IN ({marks(runs)})
			AND o.shard IN ({marks(city_values)})
			AND o.query IN ({marks(query_values)})
		ORDER BY o.observed_at, j.id
		""",
		(*runs, *city_values, *query_values),
	).fetchall()
	result = []
	for row in rows:
		platform, identity_key, platform_job_id = (str(row[1]), str(row[2]), str(row[3]))
		result.append(
			{
				"source": source,
				"source_id": source_id_value,
				"source_label": source_label_value,
				"source_database": source_database_value,
				"job_pk": int(row[0]),
				"match_key": _match_key(platform, platform_job_id, identity_key),
				"platform": platform,
				"platform_job_id": platform_job_id,
				"identity_key": identity_key,
				"title": str(row[4]), "company": str(row[5]), "city": str(row[6]),
				"district": str(row[7]), "salary": str(row[8]), "experience": str(row[9]),
                                "education": str(row[10]), "first_seen_at": str(row[11]), "query": str(row[12]),
				"shard": str(row[13]), "page": int(row[14]), "rank": int(row[15]),
				"observed_at": str(row[16]), "reposted": bool(row[17]), "local_new": bool(row[18]),
                                "ai_evidence": _payload_ai_evidence(str(row[19])),
                                "employment_type": _payload_employment_type(str(row[19])),
                                "ai_evidence_details": _payload_ai_evidence_details(str(row[19])), "run_id": str(row[20]),
				"repost_previous_job_id": str(row[21] or ""),
			}
		)
	return result


def _merge_jobs(main_rows: list[dict[str, Any]], supplement_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	grouped: dict[str, list[dict[str, Any]]] = {}
	for row in (*main_rows, *supplement_rows):
		grouped.setdefault(row["match_key"], []).append(row)
	jobs = [_merged_job(key, rows) for key, rows in grouped.items()]
	return sorted(jobs, key=lambda job: (job["latest_observed_at"], job["match_key"]), reverse=True)


def _merged_job(match_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
	by_source = {source: [row for row in rows if row["source"] == source] for source in ("main", "supplement")}
	by_source_id: dict[str, list[dict[str, Any]]] = {}
	for row in rows:
		by_source_id.setdefault(str(row["source_id"]), []).append(row)
	latest = max(rows, key=lambda row: row["observed_at"])
	main_rows, supplement_rows = by_source["main"], by_source["supplement"]
	presence = "main_and_supplement" if main_rows and supplement_rows else "main_only" if main_rows else "supplement_only"
	first_source = "main" if main_rows else "supplement"
	first_rows = by_source[first_source]
	first_seen = min(row["first_seen_at"] for row in first_rows)
	job_id = str(latest["platform_job_id"])
	return {
		"match_key": match_key,
		"platform": latest["platform"], "platform_job_id": job_id,
		"identity_key": latest["identity_key"], "title": latest["title"], "company": latest["company"],
		"city": latest["city"], "district": latest["district"], "salary": latest["salary"],
                "experience": latest["experience"], "education": latest["education"],
                "employment_type": latest["employment_type"],
		"boss_url": _canonical_boss_url(job_id),
		"presence": presence,
		"first_discovered_at": first_seen,
		"first_discovery_scope": "main_profile_history" if main_rows else "supplement_profile_local_history_only",
		# This is an event in one selected source run, not a statement that no
		# other profile, platform, or prior unobserved posting exists.
		"local_new_sources": [
			source for source, source_rows in by_source.items() if any(row["local_new"] for row in source_rows)
		],
		"local_new_source_ids": [
			source_id for source_id, source_rows in by_source_id.items() if any(row["local_new"] for row in source_rows)
		],
		# Neither selected local database proves absence from every other profile or
		# platform, including for supplement-only rows.
		"is_global_new": None,
		"ai_evidence": _unique_ai_evidence(rows),
		"ai_evidence_details": _unique_ai_evidence_details(rows),
		"ai_evidence_scope": "latest mutable payload_json in each selected source database; not an observation snapshot; exact source-field details are unavailable for older records that retained keywords only",
		"latest_observed_at": latest["observed_at"],
		"latest_observation": {key: latest[key] for key in ("source", "source_id", "query", "shard", "page", "rank", "observed_at")},
		"sources": [_source_summary(source_id, source_rows) for source_id, source_rows in by_source_id.items()],
	}


def _source_summary(source_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
	latest = max(rows, key=lambda row: row["observed_at"])
	reposted = any(row["reposted"] for row in rows)
	previous_ids = sorted({row["repost_previous_job_id"] for row in rows if row["repost_previous_job_id"]})
	return {
		"source": str(rows[0]["source"]),
		"source_id": source_id,
		"source_label": str(rows[0]["source_label"]),
		"source_database": str(rows[0]["source_database"]),
		"run_ids": sorted({row["run_id"] for row in rows}),
		"queries": sorted({row["query"] for row in rows}),
		# Keep the search city paired with its query. A job's displayed city can
		# differ, and independent city/query lists would invent a cross product.
		"positions": [
			{"city": row["shard"], "query": row["query"], "page": row["page"],
				"rank": row["rank"], "observed_at": row["observed_at"], "run_id": row["run_id"]}
			for row in rows
		],
		"first_seen_at": min(row["first_seen_at"] for row in rows),
		"latest_observed_at": latest["observed_at"],
		"reposted_evidence": reposted,
		"reposted_previous_job_ids": previous_ids,
		"reposted_previous_job_id_evidence": "recorded" if previous_ids else ("unavailable" if reposted else "none"),
                "local_new_in_selected_scan": any(row["local_new"] for row in rows),
                "employment_type": latest["employment_type"],
		"ai_evidence": _unique_ai_evidence(rows),
		"ai_evidence_details": _unique_ai_evidence_details(rows),
		"ai_evidence_scope": "latest mutable payload_json for this source database; not an observation snapshot; exact source-field details are unavailable for older records that retained keywords only",
	}


def _canonical_boss_url(job_id: str) -> str:
	if not _SAFE_JOB_ID.fullmatch(job_id):
		return ""
	url = f"https://www.zhipin.com/job_detail/{quote(job_id, safe='')}.html"
	return url if _is_canonical_boss_url(url) else ""


def _edge_url(url: str) -> str:
	if not _is_canonical_boss_url(url):
		return ""
	return f"microsoft-edge:{url}"


def _is_canonical_boss_url(url: str) -> bool:
	"""Accept only a generated direct BOSS job URL, never a redirect or scheme."""
	try:
		parts = urlsplit(url)
	except ValueError:
		return False
	return bool(
		parts.scheme == "https"
		and parts.netloc == "www.zhipin.com"
		and not parts.query
		and not parts.fragment
		and _CANONICAL_JOB_PATH.fullmatch(parts.path)
	)


def _presence_label(value: Any) -> str:
	return _PRESENCE_LABELS.get(str(value), "来源状态未知")


def _payload_ai_evidence(payload_json: str) -> tuple[str, ...]:
		"""Read only extractor-produced evidence; query text is deliberately absent."""
		try:
				payload = json.loads(payload_json)
		except (TypeError, ValueError):
				return ()
		values = payload.get("ai_evidence") if isinstance(payload, dict) else None
		if not isinstance(values, list):
				return ()
		return tuple(value.strip() for value in values if isinstance(value, str) and value.strip())


def _payload_employment_type(payload_json: str) -> str:
        """Return only a scanner-normalized or raw-platform-derived job type.

        Historical payloads may have neither field; callers must preserve that
        absence rather than inferring an employment type from other text.
        """
        try:
                payload = json.loads(payload_json)
        except (TypeError, ValueError):
                return ""
        if not isinstance(payload, dict):
                return ""
        normalized = payload.get("employment_type")
        if normalized == "实习":
                return "实习"
        return employment_type_from_raw(payload.get("jobType"))


def _payload_ai_evidence_details(payload_json: str) -> list[dict[str, str]]:
	"""Keep only compact extractor-shaped, listing-owned evidence details.

	Payloads may originate from older databases or hand-built fixtures, so this
	defensive parser never trusts a detail merely because its JSON shape looks
	plausible.  Terms must already be in the persisted keyword evidence and
	contexts are bounded, structured, and free of obvious credentials/contact
	data.  The raw payload is never copied into the delivery artifact.
	"""
	try:
		payload = json.loads(payload_json)
	except (TypeError, ValueError):
		return []
	if not isinstance(payload, dict):
		return []
	allowed_terms = {value.casefold() for value in _payload_ai_evidence(payload_json)}
	values = payload.get("ai_evidence_details")
	if not allowed_terms or not isinstance(values, list):
		return []
	result: list[dict[str, str]] = []
	seen: set[tuple[str, str, str]] = set()
	for value in values:
		if not isinstance(value, dict) or set(value) != {"term", "source_field", "context"}:
			continue
		term, source_field, context = value.get("term"), value.get("source_field"), value.get("context")
		if not isinstance(term, str) or not isinstance(source_field, str) or not isinstance(context, str):
			continue
		term, source_field = term.strip(), source_field.strip()
		context = re.sub(r"\s+", " ", context).strip()
		if (
			not term or term.casefold() not in allowed_terms or source_field not in _AI_DETAIL_FIELDS
			or not context or _AI_DETAIL_SECRET_PATTERN.search(context)
		):
			continue
		context = context[:_AI_DETAIL_CONTEXT_LIMIT]
		key = (term.casefold(), source_field, context)
		if key in seen:
			continue
		seen.add(key)
		result.append({"term": term, "source_field": source_field, "context": context})
		if len(result) >= _AI_DETAIL_MAXIMUM:
			break
	return result


def _unique_ai_evidence(rows: Iterable[dict[str, Any]]) -> list[str]:
	seen: set[str] = set()
	result: list[str] = []
	for row in rows:
		for value in row["ai_evidence"]:
			if value.casefold() not in seen:
				seen.add(value.casefold())
				result.append(value)
	return result


def _unique_ai_evidence_details(rows: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
	result: list[dict[str, str]] = []
	seen: set[tuple[str, str, str]] = set()
	for row in rows:
		for detail in row.get("ai_evidence_details", []):
			if not isinstance(detail, dict):
				continue
			term, source_field, context = detail.get("term"), detail.get("source_field"), detail.get("context")
			if not isinstance(term, str) or not isinstance(source_field, str) or not isinstance(context, str):
				continue
			key = (term.casefold(), source_field, context)
			if key in seen:
				continue
			seen.add(key)
			result.append({"term": term, "source_field": source_field, "context": context})
			if len(result) >= _AI_DETAIL_MAXIMUM:
				return result
	return result


def _ai_evidence_values(value: Any) -> tuple[str, ...]:
	return tuple(item for item in value if isinstance(item, str)) if isinstance(value, (list, tuple)) else ()


def _escape_text(value: Any) -> str:
	return html.escape(str(value or ""), quote=True)


def _district_suffix(value: Any) -> str:
	district = _escape_text(value)
	return f" / {district}" if district else ""
