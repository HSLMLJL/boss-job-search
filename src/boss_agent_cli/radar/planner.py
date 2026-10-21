from __future__ import annotations

import math
from typing import Any

from boss_agent_cli.radar.profile import RadarProfile


def build_radar_plan(profile: RadarProfile, *, max_requests: int | None = None) -> dict[str, Any]:
	"""Build a read-only worst-case scan plan without opening a browser."""
	request_budget = profile.max_requests if max_requests is None else max_requests
	if isinstance(request_budget, bool) or not isinstance(request_budget, int) or request_budget < 1:
		raise ValueError("max_requests must be a positive integer")

	page_limits = profile.city_page_limits
	search_page_limits = profile.search_page_limits
	planned_page_requests = sum(search_page_limits.values())
	delay_intervals = max(0, planned_page_requests - 1)
	min_wait_seconds = delay_intervals * profile.delay_range[0]
	max_wait_seconds = delay_intervals * profile.delay_range[1]
	request_headroom = request_budget - planned_page_requests
	minimum_invocations = max(1, math.ceil(planned_page_requests / request_budget))
	# This counts the initial invocation as well.  Keep the historical field
	# below, but expose unambiguous names for UI and automation consumers.
	worst_case_total_invocations = minimum_invocations
	worst_case_resume_invocations = worst_case_total_invocations - 1

	grouped: dict[int, list[str]] = {}
	if profile.query_page_limits:
		for search, pages in search_page_limits.items():
			grouped.setdefault(pages, []).append(search)
		page_groups = [
			{"pages": pages, "search_count": len(searches), "searches": searches}
			for pages, searches in sorted(grouped.items(), key=lambda item: item[0], reverse=True)
		]
	else:
		for city in profile.cities:
			grouped.setdefault(page_limits[city.name], []).append(city.name)
		page_groups = [
			{"pages": pages, "city_count": len(cities), "cities": cities}
			for pages, cities in sorted(grouped.items(), key=lambda item: item[0], reverse=True)
		]

	warnings: list[str] = []
	if request_headroom < 0:
		warnings.append(
			"如果每个搜索均到配置上限，按单次请求上限需总共 "
			f"{worst_case_total_invocations} 次调用，其中 {worst_case_resume_invocations} 次为续跑；"
			"平台提前结束时实际调用可能更少。"
		)
	elif request_headroom == 0 and profile.max_retries:
		warnings.append("请求预算刚好等于页面计划，没有为网络重试保留余量。")
	if max_wait_seconds > 90 * 60:
		warnings.append("仅串行请求间隔的上限估算已超过 90 分钟，尚未计入页面加载时间。")

	return {
		"profile": profile.name,
		"profile_fingerprint": profile.fingerprint,
		"boss_search_transport": profile.boss_search_transport,
		"boss_page_layout": profile.boss_page_layout,
		"max_retries": profile.max_retries,
		"queries": list(profile.queries),
		"query_count": len(profile.queries),
		"cities": [city.name for city in profile.cities],
		"city_count": len(profile.cities),
		"planned_searches": len(profile.cities) * len(profile.queries),
		"city_page_limits": page_limits,
		"search_page_limits": search_page_limits,
		"page_groups": page_groups,
		"planned_page_requests": planned_page_requests,
		"max_requests": request_budget,
		"request_headroom": request_headroom,
		"minimum_invocations_at_budget": minimum_invocations,
		"worst_case_total_invocations": worst_case_total_invocations,
		"worst_case_resume_invocations": worst_case_resume_invocations,
		"delay_range_seconds": list(profile.delay_range),
		"estimated_wait_minutes": {
			"minimum": round(min_wait_seconds / 60, 1),
			"maximum": round(max_wait_seconds / 60, 1),
		},
		"estimate_excludes": ["页面加载时间", "网络重试等待", "平台提前结束分页"],
		"warnings": warnings,
		"can_finish_in_one_invocation_without_retries": request_headroom >= 0,
		"note": "这是最坏情况观察窗口的离线估算，不访问平台；平台提前返回无更多结果时，实际页数会减少。",
	}
