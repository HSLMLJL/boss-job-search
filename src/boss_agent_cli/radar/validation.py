from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from boss_agent_cli.radar.acceptance import _natural_end_page_evidence
from boss_agent_cli.radar.evidence_scope import resolve_observation_run_ids
from boss_agent_cli.radar.planner import build_radar_plan
from boss_agent_cli.radar.profile import RadarProfile
from boss_agent_cli.radar.store import RadarStore


_CHINA_TZ = timezone(timedelta(hours=8))
_ACCOUNT_SAFETY_CODES = {
	"ACCOUNT_RISK",
	"CAPTCHA_REQUIRED",
	"ENVIRONMENT_RISK",
	"RATE_LIMITED",
	"RISK_CONTROL",
	"VERIFY_REQUIRED",
}
_ACCOUNT_SAFETY_TERMS = ("验证码", "风控", "环境异常", "账号异常", "封禁", "访问频繁")
_MANUAL_REVIEW_SPECS: dict[str, dict[str, Any]] = {
	"relevance": {
		"criterion": "相关岗位准确率不低于85%",
		"minimum_sample": 20,
		"threshold": 0.85,
		"kind": "minimum_true_rate",
		"true_label": "人工确认相关",
		"false_label": "人工确认误收",
	},
	"recall": {
		"criterion": "抽样召回率不低于80%",
		"minimum_sample": 20,
		"threshold": 0.80,
		"kind": "minimum_true_rate",
		"true_label": "在雷达中找到",
		"false_label": "人工样本中漏掉",
	},
	"old_newness": {
		"criterion": "旧岗位误报新增不超过2%",
		"minimum_sample": 50,
		"threshold": 0.02,
		"kind": "maximum_false_rate",
		"true_label": "人工确认确为新岗位",
		"false_label": "人工确认旧岗位却报新增",
	},
}


def _expected_searches(profile: RadarProfile) -> set[str]:
	return {f"{city.name} / {query}" for city in profile.cities for query in profile.queries}


def _string_set(value: Any) -> set[str]:
	return {item.strip() for item in value if isinstance(item, str) and item.strip()} if isinstance(value, list) else set()


def _window_sequence_continuous(batches: list[dict[str, Any]]) -> bool:
	"""Check the observed configured window without requiring a natural end."""
	if not batches:
		return False
	try:
		pages = [int(batch["batch"]) for batch in batches]
		prior_has_more = [bool(batch["has_more"]) for batch in batches[:-1]]
	except (KeyError, TypeError, ValueError):
		return False
	return pages == list(range(1, pages[-1] + 1)) and all(prior_has_more)


def _scope_boundary_label(stats: dict[str, Any]) -> str:
	page_caps = _string_set(stats.get("page_cap_reached_searches"))
	full_terminal = _string_set(stats.get("terminal_full_searches"))
	missing_terminal = _string_set(stats.get("terminal_evidence_missing_searches"))
	if page_caps:
		return "已完成配置观察窗口；部分搜索达到配置页上限，未证明页面或全城穷尽"
	if full_terminal:
		return "已完成配置观察窗口；终批满载，未证明页面或全城穷尽"
	if missing_terminal:
		return "已完成配置观察窗口；终点证据不足，未证明页面或全城穷尽"
	return "已完成配置观察窗口；不将页面状态解释为全城穷尽"


def _collection_scope_evidence(
	store: RadarStore,
	profile: RadarProfile,
	session: dict[str, Any],
	runs_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	"""Audit configured-window evidence for one reported logical scan.

	This is intentionally a *window* gate: terminal pages, full tails, and
	configured caps are described as boundaries, never as an assertion that a
	city's entire platform inventory was exhausted.
	"""
	run_id = str(session["completed_run_id"])
	terminal = runs_by_id.get(run_id)
	if terminal is None:
		return {"logical_scan_id": str(session["logical_scan_id"]), "status": "fail", "reason": "终端分段不在配置运行记录中"}
	stats = terminal["stats"]
	if profile.query_page_limits and stats.get("search_page_limits") != profile.search_page_limits:
		return {
			"logical_scan_id": str(session["logical_scan_id"]), "run_id": run_id,
			"status": "fail", "reason": "逐搜索词分页配置与当前 profile 不一致，不能计入连续验收",
			"boundary_label": _scope_boundary_label(stats),
		}
	latest = {
		"run_id": run_id,
		"mode": terminal["mode"],
		"status": terminal["status"],
		"started_at": terminal["started_at"],
		"completed_at": terminal["completed_at"],
		"stats_json": json.dumps(stats, ensure_ascii=False, sort_keys=True),
	}
	expected = _expected_searches(profile)
	# RadarStore normally uses tuple rows; evidence helpers intentionally require
	# named columns.  Restore the connection immediately so this read-only report
	# cannot change the store's normal caller behavior.
	previous_row_factory = store._conn.row_factory
	store._conn.row_factory = sqlite3.Row
	try:
		run_ids = resolve_observation_run_ids(store._conn, latest, stats)
		page_evidence = _natural_end_page_evidence(
			store._conn,
			run_ids=run_ids,
			expected_searches=expected,
			require_visible_state=stats.get("boss_search_transport") == "page_capture",
		)
	except ValueError as exc:
		return {
			"logical_scan_id": str(session["logical_scan_id"]), "run_id": run_id,
			"status": "fail", "reason": f"运行链证据无效：{exc}",
			"boundary_label": _scope_boundary_label(stats),
		}
	finally:
		store._conn.row_factory = previous_row_factory

	completed = _string_set(stats.get("completed_searches"))
	missing = sorted(expected - completed)
	unexpected = sorted(completed - expected)
	issues: list[str] = []
	if terminal["status"] != "completed" or stats.get("complete") is not True:
		issues.append("最终运行未完成")
	if missing or unexpected:
		issues.append("城市×词完成集合与当前配置不一致")
	if stats.get("stop_code") or stats.get("stop_reason"):
		issues.append("最终运行存在停止原因")
	if stats.get("failed_searches") not in (None, [], "") or stats.get("truncated_searches") not in (None, [], ""):
		issues.append("最终运行含失败或截断搜索")
	if stats.get("boss_search_transport") == "page_capture":
		terminal_metadata = stats.get("terminal_evidence_missing_searches")
		if not isinstance(terminal_metadata, list):
			issues.append("页面终点证据元数据缺失")
		elif any(isinstance(item, str) and item.strip() for item in terminal_metadata):
			issues.append("部分页面终点未验证")

	captures = {row["search"]: row for row in page_evidence["searches"]}
	raw_missing = sorted(key for key in expected if not captures.get(key, {}).get("batches"))
	sequence_missing = sorted(
		key for key in expected
		if captures.get(key, {}).get("batches") and not _window_sequence_continuous(captures[key]["batches"])
	)
	visible_state_missing = sorted(page_evidence["missing_visible_state"])
	if not page_evidence["table_present"]:
		issues.append("缺少逐批原始采集证据表")
	elif raw_missing:
		issues.append("部分城市×词缺少逐批原始证据")
	elif sequence_missing:
		issues.append("部分城市×词的批次序列不连续")
	elif visible_state_missing:
		issues.append("部分页面缺少同页状态证据")

	return {
		"logical_scan_id": str(session["logical_scan_id"]), "run_id": run_id,
		"run_ids": list(run_ids), "status": "pass" if not issues else "pending",
		"reason": "；".join(issues), "expected_searches": len(expected),
		"completed_searches": len(completed), "missing_searches": missing,
		"unexpected_searches": unexpected, "raw_evidence_table_present": page_evidence["table_present"],
		"raw_evidence_missing_searches": raw_missing,
		"batch_sequence_missing_searches": sequence_missing,
		"same_page_state_missing_searches": visible_state_missing,
		"boundary_label": _scope_boundary_label(stats),
		"platform_results_exhausted": False,
	}


def build_validation_report(
	store: RadarStore,
	profile: RadarProfile,
	*,
	target_days: int = 7,
) -> dict[str, Any]:
	"""Build a local-only longitudinal acceptance report."""
	if target_days < 1 or target_days > 365:
		raise ValueError("target_days must be between 1 and 365")

	sessions = store.recent_completed_scan_sessions(
		profile_fingerprint=profile.fingerprint,
		limit=max(target_days * 4, target_days),
	)
	runs = store.profile_runs(profile_fingerprint=profile.fingerprint, mode="daily")
	integrity = store.integrity_metrics()
	manual_reviews = store.validation_review_summary(profile_fingerprint=profile.fingerprint)
	scan_plan = build_radar_plan(profile)
	runs_by_id = {str(run["run_id"]): run for run in runs}

	scan_rows: list[dict[str, Any]] = []
	completed_dates: list[date] = []
	evidence_dates: list[date] = []
	collection_scope_scans: list[dict[str, Any]] = []
	completed_run_ids: set[str] = set()
	for session in sessions:
		if not session["chain_complete"]:
			continue
		run_ids = [str(value) for value in session["run_ids"]]
		segment_runs = [runs_by_id[run_id] for run_id in run_ids if run_id in runs_by_id]
		metrics = store.scan_session_page_metrics(run_ids)
		# A valid configured window can contain zero relevant jobs.  It still needs
		# a collection-scope verdict instead of disappearing from longitudinal days.
		completed_run_ids.update(run_ids)
		completed_at = _parse_time(str(session["completed_at"]))
		started_at = _parse_time(str(session["started_at"]))
		if completed_at is None:
			continue
		local_day = completed_at.astimezone(_CHINA_TZ).date()
		completed_dates.append(local_day)
		scope = _collection_scope_evidence(store, profile, session, runs_by_id)
		scope["date"] = local_day.isoformat()
		collection_scope_scans.append(scope)
		if scope["status"] == "pass":
			evidence_dates.append(local_day)
		active_seconds = sum(_number(run["stats"].get("elapsed_seconds")) for run in segment_runs)
		wall_seconds = (
			max(0.0, (completed_at - started_at).total_seconds()) if started_at is not None else active_seconds
		)
		stop_codes = [
			str(run["stats"].get("stop_code") or "")
			for run in segment_runs
			if str(run["stats"].get("stop_code") or "")
		]
		scan_rows.append(
			{
				"logical_scan_id": str(session["logical_scan_id"]),
				"date": local_day.isoformat(),
				"segment_count": len(run_ids),
				"run_ids": run_ids,
				"active_minutes": round(active_seconds / 60, 1),
				"wall_minutes": round(wall_seconds / 60, 1),
				"requests": sum(int(_number(run["stats"].get("requests"))) for run in segment_runs),
				"page_positions": len(metrics),
				"observed_jobs": sum(int(row["observed_jobs"]) for row in metrics),
				"new_jobs": sum(int(row["new_jobs"]) for row in metrics),
				"reposted_jobs": sum(int(row["reposted_jobs"]) for row in metrics),
				"stop_codes_recovered": stop_codes,
			}
		)

	unique_days = sorted(set(completed_dates))
	evidence_days = sorted(set(evidence_dates))
	longest_streak = _longest_consecutive_streak(unique_days)
	evidence_longest_streak = _longest_consecutive_streak(evidence_days)
	interruptions = []
	safe_incidents = []
	completed_day_set = set(unique_days)
	for run in runs:
		if run["status"] not in {"stopped", "failed"}:
			continue
		stats = run["stats"]
		completed_at = _parse_time(str(run["completed_at"] or run["started_at"]))
		day = completed_at.astimezone(_CHINA_TZ).date() if completed_at else None
		stop_code = str(stats.get("stop_code") or "UNKNOWN")
		stop_reason = str(stats.get("stop_reason") or "")
		item = {
			"run_id": str(run["run_id"]),
			"date": day.isoformat() if day else "",
			"stop_code": stop_code,
			"stop_reason": stop_reason,
			"recovered_same_day": bool(day and day in completed_day_set),
			"part_of_completed_scan": str(run["run_id"]) in completed_run_ids,
		}
		interruptions.append(item)
		if _is_account_safety_incident(stop_code, stop_reason):
			safe_incidents.append(item)

	duplicate_groups = integrity["duplicate_identity_groups"] + integrity["duplicate_platform_job_id_groups"]
	duplicate_rate = duplicate_groups / integrity["jobs"] if integrity["jobs"] else 0.0
	max_wall_minutes = max((float(scan["wall_minutes"]) for scan in scan_rows), default=0.0)
	scope_failures = [item for item in collection_scope_scans if item["status"] == "fail"]
	scope_gate_status = "fail" if scope_failures else ("pass" if evidence_longest_streak >= target_days else "pending")
	criteria = [
		_criterion(
			"采集范围证据",
			scope_gate_status,
			f"有原始逐批且配置范围完整的最长连续 {evidence_longest_streak}/{target_days} 天；已报告 {len(unique_days)} 天。页面终点不证明全城岗位穷尽。",
			observed_pass=evidence_longest_streak >= target_days if not scope_failures else False,
		),
		_criterion(
			"连续运行天数",
			"pass" if longest_streak >= target_days else "pending",
			f"最长连续 {longest_streak}/{target_days} 天；已完成日期 {len(unique_days)} 天",
		),
		_criterion(
			"单次扫描不超过90分钟",
			"fail" if max_wall_minutes > 90 else ("pass" if longest_streak >= target_days else "pending"),
			f"当前完整逻辑扫描最大墙钟耗时 {max_wall_minutes:.1f} 分钟",
			observed_pass=max_wall_minutes <= 90 if scan_rows else None,
		),
		_criterion(
			"账号安全",
			"fail" if safe_incidents else ("pass" if longest_streak >= target_days else "pending"),
			f"验证码/风控/封禁类事件 {len(safe_incidents)} 个",
			observed_pass=not safe_incidents,
		),
		_criterion(
			"重复行不超过1%",
			"pass" if duplicate_rate <= 0.01 else "fail",
			f"重复组 {duplicate_groups}，岗位 {integrity['jobs']}，比例 {duplicate_rate:.2%}",
			observed_pass=duplicate_rate <= 0.01,
		),
		*[_manual_review_criterion(metric, manual_reviews.get(metric)) for metric in _MANUAL_REVIEW_SPECS],
	]
	overall = "failed" if any(item["status"] == "fail" for item in criteria) else "pending"
	if criteria and all(item["status"] == "pass" for item in criteria):
		overall = "passed"
	return {
		"profile": profile.name,
		"profile_fingerprint": profile.fingerprint,
		"target_days": target_days,
		"overall_status": overall,
		"reported_days": [value.isoformat() for value in unique_days],
		"completed_days": [value.isoformat() for value in unique_days],
		"completed_day_count": len(unique_days),
		"longest_consecutive_days": longest_streak,
		"evidence_accepted_days": [value.isoformat() for value in evidence_days],
		"evidence_accepted_day_count": len(evidence_days),
		"evidence_longest_consecutive_days": evidence_longest_streak,
		# Historical callers use days_remaining for reported completed runs.  Keep
		# that compatible value, and expose the stricter evidence clock separately.
		"evidence_days_remaining": max(0, target_days - evidence_longest_streak),
		"days_remaining": max(0, target_days - longest_streak),
		"complete_logical_scans": len(scan_rows),
		"scan_plan": {
			"city_page_limits": scan_plan["city_page_limits"],
			"search_page_limits": scan_plan["search_page_limits"],
			"page_groups": scan_plan["page_groups"],
			"planned_page_requests": scan_plan["planned_page_requests"],
			"max_requests": scan_plan["max_requests"],
			"request_headroom": scan_plan["request_headroom"],
		},
		"scans": scan_rows,
		"collection_scope_scans": collection_scope_scans,
		"operational_interruptions": interruptions,
		"account_safety_incidents": safe_incidents,
		"integrity": {**integrity, "duplicate_group_rate": round(duplicate_rate, 6)},
		"criteria": criteria,
		"manual_reviews": manual_reviews,
		"manual_review_required": [
			spec["criterion"]
			for metric, spec in _MANUAL_REVIEW_SPECS.items()
			if not _manual_review_is_sufficient(manual_reviews.get(metric), minimum_sample=int(spec["minimum_sample"]))
		],
		"note": "报告只读取本地数据库。reported_days 只表示有完成运行记录；采集范围证据另需逐批原始记录与当前配置范围一致。人工复核为用户提供的 true/false 汇总，未逐条绑定岗位或运行证据；pending 不等于失败，也不会用内部一致性替代人工效果抽检。",
	}


def _criterion(
	name: str,
	status: str,
	details: str,
	*,
	observed_pass: bool | None = None,
) -> dict[str, Any]:
	return {"name": name, "status": status, "details": details, "observed_pass": observed_pass}


def _manual_review_is_sufficient(review: dict[str, Any] | None, *, minimum_sample: int) -> bool:
	return bool(review and int(review.get("sample_size") or 0) >= minimum_sample)


def _manual_review_criterion(metric: str, review: dict[str, Any] | None) -> dict[str, Any]:
	spec = _MANUAL_REVIEW_SPECS[metric]
	minimum_sample = int(spec["minimum_sample"])
	if not _manual_review_is_sufficient(review, minimum_sample=minimum_sample):
		observed = int((review or {}).get("sample_size") or 0)
		return _criterion(
			str(spec["criterion"]),
			"pending",
			f"需至少人工复核 {minimum_sample} 条；当前 {observed} 条。"
			f"true={spec['true_label']}，false={spec['false_label']}。用户提供的汇总未逐条绑定岗位或运行证据。",
		)
	true_count = int((review or {}).get("true_count") or 0)
	false_count = int((review or {}).get("false_count") or 0)
	total = true_count + false_count
	true_rate = true_count / total
	false_rate = false_count / total
	threshold = float(spec["threshold"])
	if spec["kind"] == "minimum_true_rate":
		passed = true_rate >= threshold
		details = f"用户提供的人工汇总 {total} 条，{spec['true_label']} {true_count}，{spec['false_label']} {false_count}，比例 {true_rate:.2%}；未逐条绑定岗位或运行证据。"
	else:
		passed = false_rate <= threshold
		details = f"用户提供的人工汇总 {total} 条，{spec['false_label']} {false_count}，误报率 {false_rate:.2%}；未逐条绑定岗位或运行证据。"
	return _criterion(str(spec["criterion"]), "pass" if passed else "fail", details, observed_pass=passed)


def _number(value: Any) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _parse_time(value: str) -> datetime | None:
	try:
		parsed = datetime.fromisoformat(value)
	except (TypeError, ValueError):
		return None
	return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _longest_consecutive_streak(values: list[date]) -> int:
	longest = 0
	current = 0
	previous: date | None = None
	for value in values:
		current = current + 1 if previous is not None and value == previous + timedelta(days=1) else 1
		longest = max(longest, current)
		previous = value
	return longest


def _is_account_safety_incident(code: str, reason: str) -> bool:
	if code.upper() in _ACCOUNT_SAFETY_CODES:
		return True
	text = f"{code} {reason}"
	return any(term in text for term in _ACCOUNT_SAFETY_TERMS)
