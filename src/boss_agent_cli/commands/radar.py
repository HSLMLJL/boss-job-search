from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zipfile import BadZipFile

import click
from openpyxl.utils.exceptions import InvalidFileException
from rich.table import Table

from boss_agent_cli.auth.manager import AuthManager
from boss_agent_cli.bridge.client import BridgeClient
from boss_agent_cli.compliance import require_compliance_allowed
from boss_agent_cli.commands._platform import get_platform_instance
from boss_agent_cli.display import console, handle_auth_errors, handle_error_output, handle_output
from boss_agent_cli.radar.acceptance import RadarAcceptanceError, verify_latest_radar_run
from boss_agent_cli.radar.coverage import build_coverage_report, write_coverage_report
from boss_agent_cli.radar.exporter import RadarExportError, export_radar_workbook
from boss_agent_cli.radar.planner import build_radar_plan
from boss_agent_cli.radar.profile import (
	RadarProfile,
	RadarProfileError,
	load_radar_profile,
	profile_path,
	resolve_output_dir,
)
from boss_agent_cli.radar.review_samples import RadarReviewSampleError, build_review_sample
from boss_agent_cli.radar.run_lock import RadarRunInProgressError, RadarRunLock
from boss_agent_cli.radar.safety import (
	RadarSafetyError,
	list_unresolved_radar_incidents,
	resolve_radar_incident,
)
from boss_agent_cli.radar.scanner import BossRadarSource, RadarScanner, RadarSearchPage, RadarSourceError
from boss_agent_cli.radar.store import RadarStore
from boss_agent_cli.radar.validation import build_validation_report


_CHINA_TZ = timezone(timedelta(hours=8))
_REQUIRED_RADAR_TRANSPORT = "page_capture"


def _radar_transport_blocker(profile: RadarProfile) -> str | None:
	"""Return the local-only reason a profile cannot start a radar scan."""
	if profile.boss_search_transport != _REQUIRED_RADAR_TRANSPORT:
		return (
			"岗位雷达只允许 boss_search_transport: page_capture；"
			f"当前配置为 {profile.boss_search_transport!r}，已在访问浏览器和平台前阻止。"
		)
	return None


def _radar_transport_error_data(profile: RadarProfile) -> dict[str, Any]:
	"""Provide an auditable no-request response for a rejected transport."""
	return {
		"profile": profile.name,
		"boss_search_transport": profile.boss_search_transport,
		"required_boss_search_transport": _REQUIRED_RADAR_TRANSPORT,
		"max_retries": profile.max_retries,
		"platform_requests": 0,
		"scan_started": False,
	}


def _account_risk_recovery(handoff: dict[str, Any] | None) -> str:
	prefix = "本次已停止；不要自动重试或自动换号。"
	if handoff and handoff.get("platform_code") in (32, "32"):
		return (
			prefix + "平台明确返回账号暂时禁止使用（code 32）。"
			"请用户通过 BOSS 官方账号限制处理或申诉入口处理；平台确认恢复前不要再次采集。"
			"重新加载扩展、重新登录或次日 --fresh 不能作为账号限制已解除的证据。"
		)
	if handoff and handoff.get("verification_url_available") and handoff.get("verification_url"):
		return (
			prefix + f"请用户在现有 Chrome 中打开平台返回的验证入口：{handoff['verification_url']}。"
			"验证期间保持该页不动，不刷新；用户确认结果后再决定是否恢复采集。"
		)
	return (
		prefix + "当前没有可安全直接打开的验证链接；请用户检查现有官方 BOSS 搜索页。"
		"若出现验证则由用户完成，若正常显示岗位则记录该状态；不要反复刷新或要求重新登录。"
	)


def _incomplete_scan_recovery(
	*,
	stop_code: str,
	handoff: dict[str, Any] | None,
	request_budget_exhausted: bool,
	resume_command: str,
) -> tuple[bool, str]:
	"""Describe the prerequisite for recovery, never treat a new day as a repair.

	This is output guidance, not an additional persistent safety gate. An external
	failure is not advertised as directly recoverable before its cause is checked.
	"""
	if stop_code == "ACCOUNT_RISK" or bool((handoff or {}).get("needs_browser_handoff")):
		return False, _account_risk_recovery(handoff)
	if stop_code == "USER_CANCELLED":
		return False, (
			"用户已停止扫描；本次审计和断点已保留，不要自动重新启动。"
			"仅在用户要求继续后，再根据配置、日期和网页状态决定如何恢复。"
		)
	if stop_code in {"BROWSER_ERROR", "BROWSER_SESSION_REQUIRED", "NETWORK_ERROR", "REQUEST_TIMEOUT", "TIMEOUT"}:
		return False, (
			"本次审计和断点已保留；先排查普通网页能否打开、本地 Bridge 和目标列表是否可读取。"
			"环境未恢复前不要重试、自动刷新或重新登录；跨日不会自动修复连接或导航故障。"
			"恢复后再根据配置、日期和当前网页进度决定续扫，不要为排查故障清掉断点。"
		)
	if stop_code in {"AUTH_REQUIRED", "AUTH_EXPIRED", "TOKEN_REFRESH_FAILED"}:
		return False, (
			"本次审计和断点已保留；先检查现有官方页面的登录状态，不要自动刷新或反复要求登录。"
			"确实需要登录时由用户完成并保持页面不动；确认能正常显示目标岗位后再决定续扫。"
			"不要自动换号或跨日重扫来代替登录状态恢复。"
		)
	if not stop_code and request_budget_exhausted:
		return True, (
			"本次请求预算已用完，审计和断点已保留；这不是平台没有更多岗位。"
			f"同日且当前网页仍匹配断点时，可使用 {resume_command} 续扫；"
			"跨日应开始新的观察窗口。续扫仍须通过风险、配置和网页状态检查，不要循环自动重启。"
		)
	return False, (
		"本次审计和断点已保留；先排查停止原因、页面状态或采集适配器。"
		"原因未确认前不要自动重试、换通道或跨日重扫；修复并验证后再决定如何恢复。"
	)


@click.group("radar")
def radar_group() -> None:
	"""每天查找精选城市新增的目标岗位。"""


def _radar_safety_data(data_dir: Path) -> dict[str, Any]:
	incidents = list_unresolved_radar_incidents(data_dir)
	return {
		"blocked": bool(incidents),
		"scope": "all_radar_profiles_in_data_dir",
		"unresolved_count": len(incidents),
		"latest_incident": incidents[-1] if incidents else None,
		"incidents": incidents,
		"platform_requests": 0,
		"note": "只读本地风险历史，不访问浏览器或平台；没有未解除风险不等于平台已验证可用。",
	}


@radar_group.command("safety-status")
@click.pass_context
def radar_safety_status_cmd(ctx: click.Context) -> None:
	"""只读查看跨配置的未解除风险；不访问浏览器或平台。"""
	try:
		data = _radar_safety_data(ctx.obj["data_dir"])
	except RadarSafetyError as exc:
		handle_error_output(
			ctx, "radar safety-status", code="RADAR_SAFETY_STATE_ERROR",
			message=str(exc), recoverable=False,
			recovery_action="安全状态无法可靠读取，停止采集并检查本地历史；不要删除记录来解除阻止。",
		)
		return
	handle_output(
		ctx, "radar safety-status", data,
		render=lambda value: console.print(
			f"未解除风险: {value['unresolved_count']}；" + ("采集已阻止" if value["blocked"] else "没有本地风险阻止记录")
		),
	)


@radar_group.command("safety-resolve")
@click.option("--incident", "incident_id", required=True, help="safety-status 返回的最新 incident_id")
@click.option(
	"--official-access-restored", is_flag=True,
	help="仅在本人已确认官方访问恢复后使用；禁止定时任务或代理自动代为确认",
)
@click.pass_context
def radar_safety_resolve_cmd(ctx: click.Context, incident_id: str, official_access_restored: bool) -> None:
	"""记录本人确认官方访问已恢复；不登录、不探测、不启动扫描。"""
	if not official_access_restored:
		handle_error_output(
			ctx, "radar safety-resolve", code="OFFICIAL_CONFIRMATION_REQUIRED",
			message="只有本人已确认官方访问恢复后，才能记录风险解除；不能因等待到时或更换账号自动解除。",
			recoverable=False,
		)
		return
	try:
		with RadarRunLock(ctx.obj["data_dir"] / "radar" / ".run.lock", profile="safety-resolve"):
			data = resolve_radar_incident(ctx.obj["data_dir"], incident_id)
	except (RadarSafetyError, RadarRunInProgressError) as exc:
		handle_error_output(
			ctx, "radar safety-resolve", code="RADAR_SAFETY_STATE_ERROR",
			message=str(exc), recoverable=False,
		)
		return
	data["platform_requests"] = 0
	data["scan_started"] = False
	handle_output(ctx, "radar safety-resolve", data, render=lambda _: console.print("已记录人工恢复确认；未启动采集。"))


def bundled_bridge_extension_path() -> Path:
	"""Return the installed extension directory, with a source-checkout fallback."""
	package_path = Path(__file__).resolve().parents[1] / "bridge_extension"
	if (package_path / "manifest.json").is_file():
		return package_path
	source_path = Path(__file__).resolve().parents[3] / "extension"
	if (source_path / "manifest.json").is_file():
		return source_path
	raise FileNotFoundError("当前安装包未包含 BOSS Agent Bridge 扩展")


def _radar_extension_data() -> dict[str, Any]:
	"""Compare packaged code with one local daemon status read; never navigate."""
	path = bundled_bridge_extension_path().resolve()
	manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
	version = manifest["version"]
	if not isinstance(version, str) or not version:
		raise ValueError("扩展 manifest 缺少可信版本")
	expected_hash = hashlib.sha256((path / "background.js").read_bytes()).hexdigest()
	status = BridgeClient().status()
	status = status if isinstance(status, dict) else {}
	loaded_version = status.get("extensionVersion")
	loaded_hash = status.get("extensionBuildHash")
	loaded_id = status.get("extensionId")
	valid_id = isinstance(loaded_id, str) and re.fullmatch(r"[a-p]{32}", loaded_id) is not None
	verified = bool(
		status.get("extensionConnected") is True and valid_id
		and loaded_version == version and loaded_hash == expected_hash
	)
	return {
		"verified": verified,
		"expected_path": str(path),
		"expected_version": version,
		"expected_build_hash": expected_hash,
		"extension_connected": status.get("extensionConnected") is True,
		"loaded_version": loaded_version,
		"loaded_build_hash": loaded_hash,
		"loaded_extension_id": loaded_id,
		"platform_requests": 0,
		"note": "只读取本地 daemon 状态并比对代码；版本相同不够，代码指纹必须一致。这不代表 BOSS 访问已恢复或采集已验收。",
	}


def _adopt_visible_page_batches(profile: RadarProfile) -> dict[str, tuple[tuple[int, RadarSearchPage], ...]]:
	"""Read the currently claimed, already-rendered p1/p2 list once.

	The Bridge action is fixed and read-only: it neither navigates, scrolls nor
	loads another page.  This creates a new baseline segment and deliberately
	does not claim that an interrupted historical segment was recovered.
	"""
	bridge = BridgeClient()
	status = bridge.radar_tab_status()
	tab_id = status.get("tabId") if isinstance(status, dict) else None
	url = status.get("url") if isinstance(status, dict) else None
	if status.get("prepared") is not True or type(tab_id) is not int or tab_id <= 0 or not isinstance(url, str):
		raise ValueError("可见页采纳需要已绑定的 BOSS 搜索结果页")
	parsed = urlparse(url)
	params = parse_qs(parsed.query, keep_blank_values=True)
	cities = params.get("city", [])
	queries = params.get("query", [])
	if len(cities) != 1 or len(queries) != 1:
		raise ValueError("可见页采纳需要唯一的城市和关键词")
	matches = [
		(city, query)
		for query in profile.queries
		for city in profile.cities
		if city.code == cities[0] and query == queries[0]
	]
	if len(matches) != 1:
		raise ValueError("当前可见搜索不属于此配置，已停止且不会新建扫描")
	city, query = matches[0]
	raw = bridge.adopt_visible_pages(url, tab_id=tab_id, page=2)
	data = raw.get("zpData") if isinstance(raw, dict) else None
	batches = data.get("batches") if isinstance(data, dict) else None
	if not isinstance(batches, list) or len(batches) != 2:
		raise ValueError("可见页采纳返回的批次不完整")
	converted: list[tuple[int, RadarSearchPage]] = []
	page_size: int | None = None
	for expected_page, batch in enumerate(batches, start=1):
		if not isinstance(batch, dict) or batch.get("page") != expected_page:
			raise ValueError("可见页采纳返回的页序不连续")
		items = batch.get("jobList")
		has_more = batch.get("hasMore")
		state = batch.get("pageState")
		if (
			not isinstance(items, list)
			or not items
			or type(has_more) is not bool
			or not isinstance(state, dict)
			or state.get("currentPage") != expected_page
			or type(state.get("pageSize")) is not int
			or not 1 <= state["pageSize"] <= 100
			or state.get("hasMore") is not has_more
			or state.get("identityMatched") is not True
		):
			raise ValueError("可见页采纳返回的岗位批次缺少严格页面状态")
		if not all(isinstance(item, dict) for item in items):
			raise ValueError("可见页采纳返回的岗位项无效")
		if page_size is None:
			page_size = state["pageSize"]
			if has_more is not True or len(items) != page_size:
				raise ValueError("可见页采纳的第 1 页必须是完整且仍有后续页的已结算结果")
		else:
			if state["pageSize"] != page_size:
				raise ValueError("可见页采纳的两页 pageSize 不一致")
			if has_more:
				if len(items) != page_size:
					raise ValueError("可见页采纳的非终页必须是完整页面")
			elif state.get("terminal") is not True:
				raise ValueError("可见页采纳的终页缺少明确终点证据")
		converted.append((expected_page, RadarSearchPage(
			items=tuple(items), has_more=has_more,
			terminal_evidence=state.get("terminal") is True,
			page_size=state["pageSize"], same_page_state=True,
		)))
	return {f"{city.name} / {query}": tuple(converted)}


def _visible_continuation_preflight(
    profile: RadarProfile, context: dict[str, Any]
) -> dict[str, Any]:
    """Present the exact claimed tab, then read it; never navigate or load jobs."""
    bridge = BridgeClient()
    status = bridge.radar_tab_status()
    tab_id = status.get("tabId") if isinstance(status, dict) else None
    url = status.get("url") if isinstance(status, dict) else None
    if status.get("prepared") is not True or type(tab_id) is not int or tab_id <= 0 or not isinstance(url, str):
        raise ValueError("可见页接续需要已绑定的 BOSS 结果页")
    # A browser result tab becomes document.hidden whenever the user brings
    # Codex forward.  Presenting this already-claimed tab only restores its
    # window/tab focus; the Bridge implementation first inspects it and never
    # reloads, navigates, or invokes BOSS's loader.  The subsequent fixed
    # inspection below is still the sole authority for continuation.
    presented = bridge.send_command(
        "present-radar-tab", allow_replay=False, workspace="boss-radar", tab_id=tab_id
    )
    if not presented.ok:
        raise ValueError(f"可见页接续无法呈现已绑定页面: {presented.error or '未知错误'}")
    presented_data = presented.data if isinstance(presented.data, dict) else {}
    presented_tab = presented_data.get("tab") if isinstance(presented_data.get("tab"), dict) else {}
    presented_window = presented_data.get("window") if isinstance(presented_data.get("window"), dict) else {}
    active_claimed_tab_proven = (
        presented_tab.get("id") == tab_id
        and presented_tab.get("active") is True
        and presented_window.get("focused") is True
    )
    result = bridge.send_command(
        "inspect-radar-page", allow_replay=False, workspace="boss-radar", tab_id=tab_id
    )
    if not result.ok or not isinstance(result.data, dict):
        raise ValueError(f"可见页接续预检失败: {result.error or '无有效页面状态'}")
    probe = result.data
    key = context.get("search_key")
    if not isinstance(key, str) or " / " not in key:
        raise ValueError("可见页接续缺少搜索键")
    city_name, query = key.split(" / ", 1)
    city = next((item for item in profile.cities if item.name == city_name), None)
    expected_page = context.get("previous_page")
    expected_count = context.get("job_list_count")
    page_size = context.get("page_size")
    if (
        type(expected_page) is not int
        or expected_page < 1
        or type(expected_count) is not int
        or expected_count < 1
        or type(page_size) is not int
        or not 1 <= page_size <= 100
    ):
        raise ValueError("可见页接续缺少有效的已保存页面检查点")
    # A previous scroll can complete after the old caller timed out.  Accept
    # precisely one already-rendered next page, but only when it has the exact
    # expected cumulative count and the same settled non-risk page identity.
    natural_advance = (
        probe.get("page") == expected_page + 1
        and probe.get("jobListCount") == expected_count + page_size
        and probe.get("hasMore") is True
    )
    live_page = expected_page + 1 if natural_advance else expected_page
    live_count = expected_count + page_size if natural_advance else expected_count
    if (
        city is None
        or query not in profile.queries
        or probe.get("href") != url
        or str(probe.get("city") or "") != city.code
        or probe.get("query") != query
        or probe.get("status") != "ready"
        or probe.get("readyState") != "complete"
        or probe.get("componentPresent") is not True
        or type(probe.get("page")) is not int
        or probe.get("page") != live_page
        or type(probe.get("jobListCount")) is not int
        or probe.get("jobListCount") != live_count
        or probe.get("hasMore") is not True
        or probe.get("hasMoreValid") is not True
        or probe.get("loading") is not False
        or probe.get("loadingValid") is not True
        or probe.get("moreLoading") is not False
        or probe.get("moreLoadingValid") is not True
        # Windows Chrome can retain document.hidden while the original BOSS
        # tab is active in a focused window.  The extension independently
        # rechecks that active-tab proof immediately before its one loader
        # action, so accept it here only when the preceding no-load present
        # command proved the exact claimed tab active and its window focused.
        or (
            (probe.get("visibilityState") != "visible" or probe.get("hidden") is not False)
            and not active_claimed_tab_proven
        )
        or probe.get("riskSignal") is not False
        or probe.get("accountRiskSignal") is not False
        or probe.get("captchaSignal") is not False
        or probe.get("strongLoginRequiredSignal") is not False
        or probe.get("loginSignal") is not False
    ):
        raise ValueError("当前可见页与历史断点不完全匹配；未发送加载动作")
    # A service-worker/extension reload can lose its local capture checkpoint.
    # Adopt the exact already-open cumulative list. This fixed command has no
    # navigation, refresh, scroll, or BOSS loader action, and its entire p1..N
    # batch proof is validated before it can seed the next normal page action.
    try:
        seeded = bridge.adopt_visible_pages(url, tab_id=tab_id, page=live_page)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError(f"可见页接续无法只读重建本地检查点: {exc}") from exc
    seeded_data = seeded.get("zpData") if isinstance(seeded, dict) else None
    seeded_batches = seeded_data.get("batches") if isinstance(seeded_data, dict) else None
    seeded_batch = (
        seeded_batches[live_page - 1]
        if isinstance(seeded_batches, list) and len(seeded_batches) == live_page
        else None
    )
    seeded_jobs = seeded_batch.get("jobList") if isinstance(seeded_batch, dict) else None
    seeded_state = seeded_batch.get("pageState") if isinstance(seeded_batch, dict) else None
    if (
        not isinstance(seeded_jobs, list)
        or len(seeded_jobs) != page_size
        or not isinstance(seeded_batch, dict)
        or seeded_batch.get("page") != live_page
        or seeded_batch.get("hasMore") is not True
        or not isinstance(seeded_state, dict)
        or seeded_state.get("currentPage") != live_page
        or seeded_state.get("pageSize") != page_size
        or seeded_state.get("hasMore") is not True
        or seeded_state.get("identityMatched") is not True
    ):
        raise ValueError("可见页接续首屏检查点返回不完整；未发送加载动作")
    loaded_page: tuple[int, RadarSearchPage] | None = None
    if natural_advance:
        if not all(isinstance(item, dict) for item in seeded_jobs):
            raise ValueError("已自然加载的下一页缺少严格页面证据；未发送加载动作")
        loaded_page = (
            live_page,
            RadarSearchPage(
                items=tuple(seeded_jobs), has_more=True, page_size=page_size,
                same_page_state=True,
            ),
        )
    return {
        "kind": "visible_zero_action_continuation",
        "original_run_id": context["run_id"],
        "logical_scan_id": context["logical_scan_id"],
        "profile_fingerprint": profile.fingerprint,
        "search_key": key,
        "city_code": city.code,
        "query": query,
        "tab_id": tab_id,
        "url": url,
        "checkpoint_page": live_page,
        "next_page": live_page + 1,
        "loaded_page": loaded_page,
        "evidence": {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "ready": True,
            "visible": probe.get("visibilityState") == "visible" and probe.get("hidden") is False,
            "active_claimed_tab_proven": active_claimed_tab_proven,
            "job_list_count": live_count,
        "has_more": True,
        "loading": False,
        "more_loading": False,
            "page_size_from_saved_captures": page_size,
        "extension_sequence_bootstrap": f"page_{live_page}_read_only",
            "bootstrap_job_count": len(seeded_jobs),
            "naturally_advanced_page_adopted": natural_advance,
            "prefix_validation": "extension_checkpoint_at_capture",
            "old_id_prefix_compared": False,
            "prefix_note": "固定 inspect 不返回完整历史 ID 前缀；本次仅依赖原 capture 已验证且未丢弃的扩展 checkpoint，不能声称 DB 旧前缀已重比对。",
        },
    }


def _radar_preflight_data(data_dir: Path, profile_name: str) -> dict[str, Any]:
	"""Collect only local readiness evidence before a user-authorized scan."""
	profile = load_radar_profile(data_dir, profile_name)
	database_path = data_dir / "radar" / f"{profile.name}_radar.db"
	has_completed_baseline = False
	recent_daily_execution: dict[str, Any] | None = None
	if database_path.is_file():
		with RadarStore(database_path, read_only=True) as store:
			has_completed_baseline = store.has_completed_baseline(profile.fingerprint)
			recent_daily_execution = _recent_daily_execution(store, profile)
	safety = _radar_safety_data(data_dir)
	blockers: list[str] = []
	transport_blocker = _radar_transport_blocker(profile)
	if transport_blocker:
		# An unsupported transport must not even probe the local browser bridge.
		# This keeps preflight a useful offline diagnosis for legacy profiles.
		extension: dict[str, Any] = {"verified": False, "skipped": True}
		extension_error = ""
		blockers.append(transport_blocker)
	else:
		try:
			extension = _radar_extension_data()
			extension_error = ""
		except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
			extension = {"verified": False}
			extension_error = str(exc)
	if safety["blocked"]:
		blockers.append("存在未解除的本地风险记录")
	if not extension["verified"]:
		if not transport_blocker:
			blockers.append("Browser Bridge 扩展未完成版本和代码指纹核对")
	return {
		"profile": profile.name,
		"profile_fingerprint": profile.fingerprint,
		"boss_search_transport": profile.boss_search_transport,
		"required_boss_search_transport": _REQUIRED_RADAR_TRANSPORT,
		"max_retries": profile.max_retries,
		"database_exists": database_path.is_file(),
		"has_completed_baseline": has_completed_baseline,
		"next_mode": "daily" if has_completed_baseline else "baseline",
		"scan_plan": build_radar_plan(profile),
		"recent_daily_execution": recent_daily_execution,
		"safety": safety,
		"extension": extension,
		"extension_error": extension_error,
		"local_ready": not blockers,
		"blockers": blockers,
		"platform_requests": 0,
		"scan_started": False,
		"note": (
			"预检仅核对本地条件，不登录、不导航、不读取 BOSS 页面。"
			"local_ready=true 也不等于平台已确认可访问；存在官方访问限制时必须由本人先完成官方流程。"
		),
	}


def _recent_daily_execution(store: RadarStore, profile: RadarProfile) -> dict[str, Any] | None:
	"""Summarize one completed daily scan without making a platform call."""
	sessions = store.recent_completed_scan_sessions(profile_fingerprint=profile.fingerprint, limit=1)
	if not sessions:
		return None
	session = sessions[0]
	run_ids = tuple(str(value) for value in session["run_ids"])
	summaries = [store.run_summary(run_id) for run_id in run_ids]
	completed_summary = next(
		(item for item in summaries if item["run_id"] == session["completed_run_id"]),
		summaries[-1],
	)
	logical_totals = completed_summary["stats"].get("scan_totals")
	if isinstance(logical_totals, dict):
		active_seconds = _as_nonnegative_float(logical_totals.get("elapsed_seconds"))
		requests = int(_as_nonnegative_float(logical_totals.get("requests")))
		pages = int(_as_nonnegative_float(logical_totals.get("pages")))
	else:
		active_seconds = sum(_as_nonnegative_float(item["stats"].get("elapsed_seconds")) for item in summaries)
		requests = sum(int(_as_nonnegative_float(item["stats"].get("requests"))) for item in summaries)
		pages = sum(int(_as_nonnegative_float(item["stats"].get("pages"))) for item in summaries)
	recorded_page_positions = len(store.scan_session_page_metrics(run_ids))
	wall_minutes: float | None = None
	try:
		started = datetime.fromisoformat(str(session["started_at"]))
		completed = datetime.fromisoformat(str(session["completed_at"]))
		if started.tzinfo is not None and completed.tzinfo is not None:
			wall_minutes = round(max(0.0, (completed - started).total_seconds()) / 60, 1)
	except (TypeError, ValueError):
		pass
	return {
		"logical_scan_id": str(session["logical_scan_id"]),
		"run_ids": run_ids,
		"segment_count": len(run_ids),
		"pages": pages,
		"recorded_page_positions": recorded_page_positions,
		"requests": requests,
		"active_minutes": round(active_seconds / 60, 1),
		"wall_minutes": wall_minutes,
		"note": "真实历史观测，不是未来扫描的时长承诺；最坏情况仍由分页熔断上限决定。",
	}


def _as_nonnegative_float(value: Any) -> float:
	try:
		return max(0.0, float(value or 0))
	except (TypeError, ValueError):
		return 0.0


@radar_group.command("extension-status")
@click.pass_context
def radar_extension_status_cmd(ctx: click.Context) -> None:
	"""只读核对浏览器扩展的实际版本和代码指纹，不打开或刷新网页。"""
	try:
		data = _radar_extension_data()
	except (OSError, ValueError, KeyError, TypeError) as exc:
		handle_error_output(ctx, "radar extension-status", code="EXTENSION_INTEGRITY_ERROR",
			message=str(exc), recoverable=False)
		return
	handle_output(ctx, "radar extension-status", data, render=lambda value: console.print(
		"扩展代码一致性: " + ("已核对" if value["verified"] else "尚未核对通过；不会放行页面扫描")
	))


@radar_group.command("extension-path")
@click.pass_context
def radar_extension_path_cmd(ctx: click.Context) -> None:
	"""显示 Chrome 加载已解压扩展程序时应选择的目录。"""
	try:
		path = bundled_bridge_extension_path().resolve()
	except FileNotFoundError as exc:
		handle_error_output(
			ctx,
			"radar extension-path",
			code="NOT_FOUND",
			message=str(exc),
			recoverable=True,
			recovery_action="重新安装包含 bridge_extension 的 boss-agent-cli 发布包",
		)
		return
	data = {
		"path": str(path),
		"manifest": str(path / "manifest.json"),
		"chrome_page": "chrome://extensions",
	}
	handle_output(
		ctx,
		"radar extension-path",
		data,
		render=lambda value: console.print(f"扩展目录: {value['path']}"),
		hints={"next_actions": ["在 Chrome 扩展程序页选择“加载已解压的扩展程序”，然后选择该目录"]},
	)


def _render_radar(data: dict[str, Any]) -> None:
	coverage = data["coverage"]
	console.print(
		f"[bold]岗位雷达[/bold]  配置: {data['profile']}  模式: {data['mode_label']}  "
		f"扫描 {coverage['pages']} 页 / {coverage['jobs_seen']} 条"
	)
	if not coverage["complete"]:
		failed = coverage["failed_cities"] or ["存在未完成的城市/搜索词组合"]
		console.print(f"[yellow]覆盖未完成[/yellow]：{', '.join(failed)}")
	elif coverage["page_cap_reached_searches"]:
		console.print(
			f"[yellow]观察窗口已完成，但 {len(coverage['page_cap_reached_searches'])} 个搜索组合在页数上限后仍有更多结果[/yellow]"
		)
	exports = data.get("exports", {})
	if exports.get("latest_xlsx"):
		console.print(f"Excel: {exports['latest_xlsx']}")
	verification = data.get("verification", {})
	if verification.get("performed"):
		console.print(
			f"[green]自动验收通过[/green]："
			f"{verification['checks_passed']} / {verification['checks_total']} 项"
		)
	elif verification:
		console.print("[yellow]自动验收待完成[/yellow]：本次覆盖不完整，已保留审计文件")
	jobs = data["new_jobs"]
	if not jobs:
		console.print(data["note"])
		return
	table = Table(title=f"今日新增 / 疑似重发（{len(jobs)}）")
	table.add_column("类型")
	table.add_column("岗位")
	table.add_column("公司")
	table.add_column("城市")
	table.add_column("薪资")
	table.add_column("链接", overflow="fold")
	for job in jobs:
		table.add_row(
			job["event_type"],
			job["title"],
			job["company"],
			" ".join(item for item in (job["city"], job["district"]) if item),
			job["salary"],
			job["url"],
		)
	console.print(table)


def _render_depth_report(data: dict[str, Any]) -> None:
	console.print(
		f"[bold]岗位雷达页深报告[/bold]  配置: {data['profile']}  "
		f"纳入 {data['runs_included']} 次完整 daily 扫描"
	)
	if not data["pages"]:
		console.print(data["note"])
		return
	table = Table(title="按城市 / 搜索词 / 页码累计")
	table.add_column("城市")
	table.add_column("搜索词")
	table.add_column("页码", justify="right")
	table.add_column("覆盖运行", justify="right")
	table.add_column("观察岗位", justify="right")
	table.add_column("新增", justify="right")
	table.add_column("疑似重发", justify="right")
	for row in data["pages"]:
		table.add_row(
			row["city"],
			row["query"],
			str(row["page"]),
			str(row["runs_observed"]),
			str(row["observed_jobs"]),
			str(row["new_jobs"]),
			str(row["reposted_jobs"]),
		)
	console.print(table)
	console.print(data["note"])


def _render_acceptance_report(data: dict[str, Any]) -> None:
	status = "[green]通过[/green]" if data["passed"] else "[red]未通过[/red]"
	console.print(
		f"[bold]岗位雷达验收[/bold]  {status}  配置: {data['profile']}  "
		f"模式: {data['mode']}  检查: {data['checks_passed']} / {data['checks_total']}"
	)
	console.print(f"运行ID: {data['run_id']}")
	console.print(f"Excel: {data['workbook']}")
	if data.get("verification_scope") == "natural_end":
		console.print("验收范围：配置内各搜索的当前列表终点及数据一致性，不证明全城搜全。")
	else:
		console.print("验收范围：配置观察窗口的数据一致性，不代表搜索已到自然终点。")
	if not data.get("visible_list_end_verified"):
		console.print("[yellow]尚未证实各搜索的当前列表终点；--require-natural-end 核验逐批证据，不证明全城搜全。[/yellow]")
	if data.get("legacy_coverage_wording"):
		console.print("[yellow]旧工作簿的‘完整’等表述仅作历史记录，不作为覆盖结论。[/yellow]")
	if data["passed"]:
		console.print("数据库、Excel、链接、去重、覆盖和逐运行页码记录一致。")
		return
	table = Table(title="未通过检查")
	table.add_column("检查")
	table.add_column("详情", overflow="fold")
	for item in data["checks"]:
		if not item["passed"]:
			table.add_row(str(item["name"]), str(item["details"]))
	console.print(table)


def _render_radar_plan(data: dict[str, Any]) -> None:
	wait = data["estimated_wait_minutes"]
	console.print(
		f"[bold]岗位雷达扫描计划[/bold]  配置: {data['profile']}  "
		f"{data['city_count']} 城 × {data['query_count']} 个搜索词"
	)
	mixed_queries = any("searches" in group for group in data["page_groups"])
	table = Table(title="城市与搜索词分页深度" if mixed_queries else "城市分页深度")
	table.add_column("每组合页数", justify="right")
	table.add_column("组合数" if mixed_queries else "城市数", justify="right")
	table.add_column("城市 / 搜索词" if mixed_queries else "城市")
	for group in data["page_groups"]:
		members = group.get("searches", group.get("cities", []))
		table.add_row(str(group["pages"]), str(len(members)), "、".join(members))
	console.print(table)
	console.print(
		f"最坏情况页面请求: {data['planned_page_requests']}；请求上限: {data['max_requests']}；"
		f"余量: {data['request_headroom']}"
	)
	console.print(
		f"仅串行间隔估算: {wait['minimum']:.1f}–{wait['maximum']:.1f} 分钟（不含页面加载和重试）"
	)
	for warning in data["warnings"]:
		console.print(f"[yellow]注意[/yellow]：{warning}")
	if data.get("output_dir"):
		console.print(f"输出目录: {data['output_dir']}")
	if data.get("output_scope") == "temporary_page_override":
		console.print("临时页数覆盖的文件单独保存，不替换正常配置的 latest.xlsx。")


def _render_radar_preflight(data: dict[str, Any]) -> None:
	state = "本地条件通过" if data["local_ready"] else "未通过"
	console.print(
		f"[bold]岗位雷达预检[/bold]  {state}  配置: {data['profile']}  "
		f"下一次模式: {data['next_mode']}"
	)
	console.print(
		f"采集通道: {data['boss_search_transport']}（要求 {data['required_boss_search_transport']}）；"
		f"自动重试: {data['max_retries']} 次"
	)
	if data["blockers"]:
		for blocker in data["blockers"]:
			console.print(f"[yellow]阻止项[/yellow]：{blocker}")
	if data["extension_error"]:
		console.print(f"[yellow]扩展状态读取失败[/yellow]：{data['extension_error']}")
	recent = data.get("recent_daily_execution")
	if recent:
		wall = recent["wall_minutes"]
		wall_text = f"，墙钟 {wall:.1f} 分钟" if isinstance(wall, (int, float)) else ""
		console.print(
			f"最近完整 daily 实测：{recent['pages']} 页，"
			f"{recent['requests']} 次请求，活动 {recent['active_minutes']:.1f} 分钟{wall_text}。"
		)
	console.print(data["note"])


def _render_validation_report(data: dict[str, Any]) -> None:
	status_labels = {"passed": "[green]通过[/green]", "pending": "[yellow]待积累[/yellow]", "failed": "[red]失败[/red]"}
	console.print(
		f"[bold]岗位雷达连续验收[/bold]  {status_labels[data['overall_status']]}  配置: {data['profile']}  "
		f"连续 {data['longest_consecutive_days']}/{data['target_days']} 天"
	)
	plan = data["scan_plan"]
	page_plan = "；".join(
		f"{group['pages']} 页：{'、'.join(group.get('searches', group.get('cities', [])))}"
		for group in plan["page_groups"]
	)
	console.print(
		f"当前分页配置：{page_plan}；最坏页面请求 {plan['planned_page_requests']}，"
		f"请求上限 {plan['max_requests']}（余量 {plan['request_headroom']}）"
	)
	table = Table(title="验收指标")
	table.add_column("指标")
	table.add_column("状态")
	table.add_column("证据", overflow="fold")
	for item in data["criteria"]:
		table.add_row(item["name"], item["status"], item["details"])
	console.print(table)
	if data["scans"]:
		latest = data["scans"][0]
		console.print(
			f"最近完整扫描 {latest['date']}：{latest['segment_count']} 段，"
			f"活动 {latest['active_minutes']:.1f} 分钟，墙钟 {latest['wall_minutes']:.1f} 分钟，"
			f"新增 {latest['new_jobs']}，疑似重发 {latest['reposted_jobs']}。"
		)
	console.print(data["note"])


def _render_validation_review(data: dict[str, Any]) -> None:
	console.print(
		f"已记录人工复核：{data['metric_label']}，样本 {data['sample_size']} 条，"
		f"true {data['true_count']} / false {data['false_count']}。"
	)


def _render_review_sample(data: dict[str, Any]) -> None:
	console.print(
		f"[bold]岗位雷达人工抽检样本[/bold]  {data['metric_label']}  "
		f"{data['sample_size']} / {data['requested_count']} 条"
	)
	console.print(data["instruction"])
	table = Table(title=f"最近完整扫描 {data['logical_scan_id']} 的本地样本")
	table.add_column("序号", justify="right")
	table.add_column("岗位")
	table.add_column("公司")
	table.add_column("城市")
	table.add_column("薪资")
	table.add_column("AI证据")
	table.add_column("链接", overflow="fold")
	for job in data["jobs"]:
		table.add_row(
			str(job["sample_no"]),
			str(job["title"]),
			str(job["company"]),
			"·".join(value for value in (job["city"], job["district"]) if value),
			str(job["salary"]),
			"、".join(str(item) for item in job["ai_evidence"]),
			str(job["url"]),
		)
	console.print(data["note"])


@radar_group.command("plan")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.option("--max-pages", default=None, type=click.IntRange(1, 100), help="临时按统一页数估算全部城市")
@click.option("--max-requests", default=None, type=click.IntRange(1, 1000), help="临时按指定请求上限估算")
@click.pass_context
def radar_plan_cmd(
	ctx: click.Context,
	profile_name: str,
	max_pages: int | None,
	max_requests: int | None,
) -> None:
	"""离线查看页数、请求预算和串行等待时间，不访问平台。"""
	data_dir = ctx.obj["data_dir"]
	try:
		profile = load_radar_profile(data_dir, profile_name)
		if max_pages is not None:
			profile = profile.with_uniform_page_limit(max_pages)
		plan = build_radar_plan(profile, max_requests=max_requests)
		plan["output_dir"] = str(resolve_output_dir(data_dir, profile).resolve())
		plan["output_scope"] = "temporary_page_override" if max_pages is not None else "configured_profile"
	except (RadarProfileError, ValueError) as exc:
		handle_error_output(
			ctx,
			"radar plan",
			code="INVALID_PARAM",
			message=str(exc),
			recoverable=False,
		)
		return
	run_command = f"boss radar run --profile {profile.name}"
	if max_pages is not None:
		run_command += f" --max-pages {max_pages}"
	if max_requests is not None:
		run_command += f" --max-requests {max_requests}"
	handle_output(
		ctx,
		"radar plan",
		plan,
		render=_render_radar_plan,
		hints={"next_actions": [run_command]},
	)


@radar_group.command("validation-report")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.option("--days", "target_days", default=7, show_default=True, type=click.IntRange(1, 365), help="连续验收目标天数")
@click.pass_context
def radar_validation_report_cmd(ctx: click.Context, profile_name: str, target_days: int) -> None:
	"""只读汇总连续运行、耗时、安全和待人工抽检项。"""
	data_dir = ctx.obj["data_dir"]
	try:
		profile = load_radar_profile(data_dir, profile_name)
	except RadarProfileError as exc:
		handle_error_output(
			ctx,
			"radar validation-report",
			code="INVALID_PARAM",
			message=str(exc),
			recoverable=False,
		)
		return
	database_path = data_dir / "radar" / f"{profile.name}_radar.db"
	if not database_path.is_file():
		handle_error_output(
			ctx,
			"radar validation-report",
			code="NOT_FOUND",
			message=f"尚无岗位雷达数据库: {database_path}",
			recoverable=True,
			recovery_action=f"先运行 boss radar run --profile {profile.name}",
		)
		return
	with RadarStore(database_path, read_only=True) as store:
		report = build_validation_report(store, profile, target_days=target_days)
	handle_output(ctx, "radar validation-report", report, render=_render_validation_report)


@radar_group.command("review-sample")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.option(
	"--metric",
	type=click.Choice(["relevance", "old_newness"]),
	required=True,
	help="relevance=相关性，old_newness=新增是否实际为新岗位；recall 需独立外部样本，不能由本命令伪造",
)
@click.option("--count", type=click.IntRange(1, 100), default=None, help="抽取数量；默认相关性20、新增真实性50")
@click.pass_context
def radar_review_sample_cmd(
	ctx: click.Context,
	profile_name: str,
	metric: str,
	count: int | None,
) -> None:
	"""只读生成按城市均衡的人工抽检样本，不访问浏览器或平台。"""
	data_dir = ctx.obj["data_dir"]
	try:
		profile = load_radar_profile(data_dir, profile_name)
		database_path = data_dir / "radar" / f"{profile.name}_radar.db"
		if not database_path.is_file():
			raise FileNotFoundError(f"尚无岗位雷达数据库: {database_path}")
		with RadarStore(database_path, read_only=True) as store:
			data = build_review_sample(store, profile, metric=metric, count=count)
	except RadarProfileError as exc:
		handle_error_output(ctx, "radar review-sample", code="INVALID_PARAM", message=str(exc), recoverable=False)
		return
	except FileNotFoundError as exc:
		handle_error_output(
			ctx, "radar review-sample", code="NOT_FOUND", message=str(exc), recoverable=True,
			recovery_action=f"先完成 boss radar run --profile {profile_name} 的 daily 扫描",
		)
		return
	except (RadarReviewSampleError, ValueError, sqlite3.Error) as exc:
		handle_error_output(ctx, "radar review-sample", code="REVIEW_SAMPLE_UNAVAILABLE", message=str(exc), recoverable=False)
		return
	handle_output(
		ctx,
		"radar review-sample",
		data,
		render=_render_review_sample,
		hints={"next_actions": [
			"人工抽检后，使用 boss radar record-review --profile <配置> 记录 true/false 汇总",
		]},
	)


@radar_group.command("record-review")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.option(
	"--metric",
	type=click.Choice(["relevance", "recall", "old_newness"]),
	required=True,
	help="relevance=相关性，recall=抽样召回，old_newness=新增是否实际为新岗位",
)
@click.option("--true-count", type=click.IntRange(0, None), required=True, help="该指标中人工确认 true 的数量")
@click.option("--false-count", type=click.IntRange(0, None), required=True, help="该指标中人工确认 false 的数量")
@click.option(
	"--review-date",
	type=click.DateTime(formats=["%Y-%m-%d"]),
	default=lambda: datetime.now(_CHINA_TZ),
	show_default="今天（北京时间）",
	help="抽检日期，格式 YYYY-MM-DD",
)
@click.option("--note", default="", help="可选：记录抽检来源或口径，不写入平台")
@click.pass_context
def radar_record_review_cmd(
	ctx: click.Context,
	profile_name: str,
	metric: str,
	true_count: int,
	false_count: int,
	review_date: datetime,
	note: str,
) -> None:
	"""写入人工抽检汇总；不读取浏览器或发起平台请求。"""
	data_dir = ctx.obj["data_dir"]
	try:
		profile = load_radar_profile(data_dir, profile_name)
		database_path = data_dir / "radar" / f"{profile.name}_radar.db"
		if not database_path.is_file():
			raise FileNotFoundError(f"尚无岗位雷达数据库: {database_path}")
		with RadarRunLock(data_dir / "radar" / ".run.lock", profile="record-review"):
			with RadarStore(database_path) as store:
				data = store.record_validation_review(
					profile_fingerprint=profile.fingerprint,
					metric=metric,
					true_count=true_count,
					false_count=false_count,
					review_date=review_date.date(),
					note=note,
				)
	except RadarProfileError as exc:
		handle_error_output(ctx, "radar record-review", code="INVALID_PARAM", message=str(exc), recoverable=False)
		return
	except FileNotFoundError as exc:
		handle_error_output(
			ctx, "radar record-review", code="NOT_FOUND", message=str(exc), recoverable=True,
			recovery_action=f"先完成 boss radar run --profile {profile_name} 的首次基线",
		)
		return
	except (PermissionError, RadarRunInProgressError, ValueError, sqlite3.Error) as exc:
		handle_error_output(ctx, "radar record-review", code="VALIDATION_REVIEW_FAILED", message=str(exc), recoverable=False)
		return
	metric_labels = {
		"relevance": "相关岗位准确率",
		"recall": "抽样召回率",
		"old_newness": "旧岗位误报新增率",
	}
	data["metric_label"] = metric_labels[metric]
	data["scan_started"] = False
	handle_output(
		ctx,
		"radar record-review",
		data,
		render=_render_validation_review,
		hints={"next_actions": [f"boss radar validation-report --profile {profile.name}"]},
	)


@radar_group.command("preflight")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.pass_context
def radar_preflight_cmd(ctx: click.Context, profile_name: str) -> None:
	"""离线核对基线、风险和扩展一致性；不启动扫描。"""
	try:
		data = _radar_preflight_data(ctx.obj["data_dir"], profile_name)
	except RadarProfileError as exc:
		handle_error_output(ctx, "radar preflight", code="INVALID_PARAM", message=str(exc), recoverable=False)
		return
	except (OSError, sqlite3.Error, ValueError) as exc:
		handle_error_output(ctx, "radar preflight", code="PREFLIGHT_FAILED", message=str(exc), recoverable=False)
		return
	handle_output(ctx, "radar preflight", data, render=_render_radar_preflight)


@radar_group.command("verify")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.option("--max-pages", default=None, type=click.IntRange(1, 100), help="核对同样临时页数覆盖下的最新运行与独立输出目录")
@click.option(
	"--expect-mode",
	type=click.Choice(["any", "baseline", "daily"]),
	default="any",
	show_default=True,
	help="要求最新运行处于指定模式",
)
@click.option("--require-natural-end", is_flag=True, help="要求所有配置搜索已到自然终点；仍有更多或缺少终点证据时验收失败")
@click.pass_context
def radar_verify_cmd(
	ctx: click.Context, profile_name: str, max_pages: int | None,
	expect_mode: str, require_natural_end: bool,
) -> None:
	"""只读核对选定配置与输出范围内最新运行的数据库和 Excel。"""
	data_dir = ctx.obj["data_dir"]
	try:
		profile = load_radar_profile(data_dir, profile_name)
		if max_pages is not None:
			profile = profile.with_uniform_page_limit(max_pages)
		database_path = data_dir / "radar" / f"{profile.name}_radar.db"
		workbook_path = resolve_output_dir(data_dir, profile) / "latest.xlsx"
		report = verify_latest_radar_run(
			database_path=database_path,
			workbook_path=workbook_path,
			profile=profile,
			expected_mode=expect_mode,
			require_natural_end=require_natural_end,
			output_scope="temporary_page_override" if max_pages is not None else "configured_profile",
		)
	except RadarProfileError as exc:
		handle_error_output(
			ctx,
			"radar verify",
			code="INVALID_PARAM",
			message=str(exc),
			recoverable=False,
		)
		raise SystemExit(1)
	except (RadarAcceptanceError, OSError, ValueError, sqlite3.Error, BadZipFile, InvalidFileException) as exc:
		handle_error_output(
			ctx,
			"radar verify",
			code="VERIFY_FAILED",
			message=str(exc),
			recoverable=False,
		)
		raise SystemExit(1)

	handle_output(
		ctx,
		"radar verify",
		report,
		render=_render_acceptance_report,
		hints={
			"next_actions": (
				[f"打开 {workbook_path.resolve()} 查看结果"]
				if report["passed"]
				else ["根据 failed_checks 修复或重新完成不完整扫描后再验收"]
			)
		},
	)
	if not report["passed"]:
		raise SystemExit(2)


@radar_group.command("coverage-report")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True)
@click.option("--output", type=click.Path(path_type=Path), help="可选：保存 .json 和同名 .md 覆盖证据报告")
@click.pass_context
def radar_coverage_report_cmd(ctx: click.Context, profile_name: str, output: Path | None) -> None:
	"""离线核验原始加载批次，不访问浏览器，不用去重数推断搜全。"""
	try:
		profile = load_radar_profile(ctx.obj["data_dir"], profile_name)
		database = ctx.obj["data_dir"] / "radar" / f"{profile.name}_radar.db"
		data = build_coverage_report(database, profile)
		if output is not None:
			data["report_files"] = write_coverage_report(data, output)
	except (ValueError, OSError, sqlite3.Error) as exc:
		handle_error_output(ctx, "radar coverage-report", code="COVERAGE_AUDIT_FAILED", message=str(exc), recoverable=False)
		return
	handle_output(ctx, "radar coverage-report", data, render=lambda report: console.print(report))


@radar_group.command("depth-report")
@click.option("--profile", "profile_name", default="ai_pm", show_default=True, help="岗位雷达配置名")
@click.option("--runs", default=7, show_default=True, type=click.IntRange(1, 365), help="汇总最近多少次完整 daily 运行")
@click.pass_context
def radar_depth_report_cmd(ctx: click.Context, profile_name: str, runs: int) -> None:
	"""只读汇总历史运行中各页的岗位与新增贡献。"""
	data_dir = ctx.obj["data_dir"]
	try:
		profile = load_radar_profile(data_dir, profile_name)
	except RadarProfileError as exc:
		handle_error_output(
			ctx,
			"radar depth-report",
			code="INVALID_PARAM",
			message=str(exc),
			recoverable=False,
		)
		return
	database_path = data_dir / "radar" / f"{profile.name}_radar.db"
	if not database_path.is_file():
		handle_error_output(
			ctx,
			"radar depth-report",
			code="NOT_FOUND",
			message=f"尚无岗位雷达数据库: {database_path}",
			recoverable=True,
			recovery_action=f"先运行 boss radar run --profile {profile.name}",
		)
		return

	with RadarStore(database_path, read_only=True) as store:
		recent_sessions = store.recent_completed_scan_sessions(
			profile_fingerprint=profile.fingerprint,
			limit=runs,
		)
		aggregated: dict[tuple[str, str, int], dict[str, int]] = {}
		included_run_ids: list[str] = []
		included_scan_ids: list[str] = []
		skipped_scan_ids: list[str] = []
		for session in recent_sessions:
			scan_id = str(session["logical_scan_id"])
			if not session["chain_complete"]:
				skipped_scan_ids.append(scan_id)
				continue
			metrics = store.scan_session_page_metrics(session["run_ids"])
			if not metrics:
				skipped_scan_ids.append(scan_id)
				continue
			included_scan_ids.append(scan_id)
			included_run_ids.extend(str(run_id) for run_id in session["run_ids"])
			for metric in metrics:
				key = (str(metric["shard"]), str(metric["query"]), int(metric["page"]))
				bucket = aggregated.setdefault(
					key,
					{"runs_observed": 0, "observed_jobs": 0, "new_jobs": 0, "reposted_jobs": 0},
				)
				bucket["runs_observed"] += 1
				for field in ("observed_jobs", "new_jobs", "reposted_jobs"):
					bucket[field] += int(metric[field])

	city_order = {city.name: index for index, city in enumerate(profile.cities)}
	query_order = {query: index for index, query in enumerate(profile.queries)}
	pages = [
		{
			"city": city,
			"query": query,
			"page": page,
			**counts,
		}
		for (city, query, page), counts in sorted(
			aggregated.items(),
			key=lambda item: (
				city_order.get(item[0][0], len(city_order)),
				query_order.get(item[0][1], len(query_order)),
				item[0][2],
			),
		)
	]
	note = (
		"这是本地历史统计；页码不等于发布时间。积累多次真实运行后，再按各页新增贡献决定扫描深度。"
		if pages
		else "尚无带逐运行页码历史的完整 daily 扫描；完成下一次真实 daily 后再查看。"
	)
	data = {
		"profile": profile.name,
		"runs_requested": runs,
		"runs_available": len(recent_sessions),
		"runs_included": len(included_scan_ids),
		"logical_scan_ids": included_scan_ids,
		"run_ids": included_run_ids,
		"runs_skipped_without_page_history": skipped_scan_ids,
		"logical_scans": recent_sessions,
		"pages": pages,
		"note": note,
	}
	handle_output(ctx, "radar depth-report", data, render=_render_depth_report)


@radar_group.command("run")
@click.option(
	"--profile", "profile_name", default="ai_pm", show_default=True, help="使用的数据目录 radar/profiles 下的配置名"
)
@click.option("--max-pages", default=None, type=click.IntRange(1, 100), help="覆盖配置中的每个城市/关键词分页上限")
@click.option("--max-requests", default=None, type=click.IntRange(1, 1000), help="覆盖配置中的本次请求硬上限")
@click.option("--fresh", is_flag=True, help="新建一轮完整扫描并从第 1 页开始，不继承中断断点")
@click.option(
	"--adopt-visible-pages",
	is_flag=True,
	help="将当前已加载的第 1、2 页只读写入一个新基线；不导航、不重搜这两页，随后从第 3 页继续",
)
@click.option("--continue-run-id", default=None, help="显式接续一个最多 18 小时的 daily 预算中断 run；跨日不会自动恢复")
@click.option(
        "--continue-visible-run-id",
        default=None,
        help="仅接续最新 baseline 的明确页面不可见或扩展完整性零动作中断；不会自动呈现、刷新或重试",
)
@click.option(
	"--reuse-visible-first-page",
	is_flag=True,
	help="仅本次首个配置搜索复用已验证的可见第 1 页；其余搜索仍正常新建第 1 页",
)
@click.option(
        "--collect-only",
        is_flag=True,
        help="仅采集并保存数据库，不生成旧版 Excel 或执行 Excel 自动验收",
)
@click.pass_context
@handle_auth_errors("radar")
def radar_run_cmd(
	ctx: click.Context,
	profile_name: str,
	max_pages: int | None,
	max_requests: int | None,
	fresh: bool,
	adopt_visible_pages: bool,
	continue_run_id: str | None,
	continue_visible_run_id: str | None,
	reuse_visible_first_page: bool,
        collect_only: bool,
) -> None:
	"""首次建立精选城市基线；以后只输出首次发现和疑似重发岗位。"""
	if ctx.obj.get("platform", "zhipin") != "zhipin":
		handle_error_output(
			ctx,
			"radar",
			code="PLATFORM_NOT_SUPPORTED",
			message="初版 AI 产品经理岗位雷达仅支持 BOSS 直聘",
			recoverable=False,
		)
		return
	if not require_compliance_allowed(ctx, "radar-run"):
		ctx.exit(1)

	data_dir = ctx.obj["data_dir"]
	logger = ctx.obj["logger"]
	if sum(bool(value) for value in (fresh, adopt_visible_pages, continue_run_id, continue_visible_run_id)) > 1:
		handle_error_output(
			ctx,
			"radar",
			code="INVALID_PARAM",
			message="--fresh、--adopt-visible-pages、--continue-run-id 与 --continue-visible-run-id 不能组合",
			recoverable=False,
		)
		return
	if (adopt_visible_pages or continue_visible_run_id) and reuse_visible_first_page:
		handle_error_output(
			ctx,
			"radar",
			code="INVALID_PARAM",
			message="可见页采纳/接续与 --reuse-visible-first-page 不能组合",
			recoverable=False,
		)
		return
	try:
		profile = load_radar_profile(data_dir, profile_name)
	except RadarProfileError as exc:
		handle_error_output(
			ctx,
			"radar",
			code="INVALID_PARAM",
			message=str(exc),
			recoverable=False,
			recovery_action=f"检查 {data_dir / 'radar' / 'profiles'} 下的 YAML 配置",
		)
		return
	transport_blocker = _radar_transport_blocker(profile)
	if transport_blocker:
		handle_error_output(
			ctx,
			"radar",
			code="RADAR_TRANSPORT_UNSUPPORTED",
			message=transport_blocker,
			recoverable=False,
			recovery_action=(
				"在配置 YAML 中显式设置 boss_search_transport: page_capture 后再运行。"
				"--fresh、--max-pages、--max-requests 和 --reuse-visible-first-page 不能绕过此限制。"
			),
			data=_radar_transport_error_data(profile),
		)
		return
	# Page depth is part of the baseline identity. A shallow command-line smoke
	# run must not unlock daily mode for the deeper configured profile.
	if max_pages is not None:
		profile = profile.with_uniform_page_limit(max_pages)
	verify_command = f"boss radar verify --profile {profile.name}"
	if max_pages is not None:
		verify_command += f" --max-pages {max_pages}"
	verification_report: dict[str, Any] | None = None
	verification_error: str | None = None
	try:
		with RadarRunLock(data_dir / "radar" / ".run.lock", profile=profile.name):
			safety = _radar_safety_data(data_dir)
			if safety["blocked"]:
				handle_error_output(
					ctx, "radar", code="RADAR_SAFETY_BLOCKED",
					message="历史风险尚未解除，本次已在本地阻止；没有打开浏览器或请求平台。",
					recoverable=False,
					recovery_action=(
						"先运行 boss radar safety-status 查看原因。等待到时、--fresh、换配置或换通道不会解除。"
						"仅在用户明确确认官方访问已恢复后，才可记录风险解除；不得自动代为确认。"
					),
					data=safety,
				)
				return
			if profile.boss_search_transport == "page_capture":
				try:
					extension = _radar_extension_data()
				except (OSError, ValueError, KeyError, TypeError) as exc:
					handle_error_output(ctx, "radar", code="EXTENSION_INTEGRITY_ERROR",
						message=str(exc), recoverable=False)
					return
				if not extension["verified"]:
					handle_error_output(
						ctx, "radar", code="EXTENSION_BUILD_MISMATCH",
						message="扩展实际代码尚未核对通过，已在访问 BOSS 前停止。",
						recoverable=False, data=extension,
						recovery_action="先用 boss radar extension-status 核对本地 daemon 和已加载扩展；不要刷新 BOSS 或重新登录来修复版本问题。",
					)
					return
			auth = AuthManager(data_dir, logger=logger, platform="zhipin")
			with RadarStore(data_dir / "radar" / f"{profile.name}_radar.db") as store:
				# The OS lock proves no live radar process owns these rows. Preserve
				# their checkpoints, mark them terminal, then resume normally.
				store.recover_running_runs()
				visible_batches: dict[str, tuple[tuple[int, RadarSearchPage], ...]] | None = None
				visible_continuation_context: dict[str, Any] | None = None
				if continue_visible_run_id:
					try:
						visible_continuation_context = store.explicit_visible_continuation_context(
							run_id=continue_visible_run_id,
							profile_fingerprint=profile.fingerprint,
						)
						visible_metadata = _visible_continuation_preflight(
								profile, visible_continuation_context
						)
						loaded_page = visible_metadata.pop("loaded_page", None)
						if loaded_page is not None:
								visible_continuation_context["adopted_loaded_page"] = loaded_page
						# Preserve any strictly-audited local protocol refusal
						# that occurred after the real capture anchor.  It did
						# not produce a page or observation, but must remain
						# visible in the resulting continuation lineage.
						visible_metadata["intervening_zero_action_run_ids"] = list(
								visible_continuation_context.get("intervening_zero_action_run_ids", [])
						)
						visible_continuation_context["metadata"] = visible_metadata
					except (OSError, RuntimeError, TypeError, ValueError) as exc:
						handle_error_output(
							ctx,
							"radar",
							code="RADAR_VISIBLE_CONTINUATION_REJECTED",
							message=str(exc),
							recoverable=False,
							recovery_action="不要刷新、呈现或重试页面；仅在当前页已由用户显式呈现且固定预检完全匹配时再次执行此命令。",
						)
						return
				if adopt_visible_pages:
					try:
						# This fixed bridge action only reads the claimed, loaded result
						# page. Do it before the platform session exists: a rejection can
						# neither navigate nor close a radar tab.
						visible_batches = _adopt_visible_page_batches(profile)
					except (OSError, RuntimeError, TypeError, ValueError) as exc:
						handle_error_output(
							ctx,
							"radar",
							code="RADAR_VISIBLE_ADOPTION_REJECTED",
							message=str(exc),
							recoverable=False,
							recovery_action="保持当前 BOSS 页面不刷新；确认它仍是同一配置的已结算第 2 页后，再显式重试此命令。",
						)
						return
				if continue_run_id:
					try:
						# Validate before constructing an authenticated browser platform.
						store.explicit_daily_continuation_context(
							run_id=continue_run_id,
							profile_fingerprint=profile.fingerprint,
						)
					except ValueError as exc:
						handle_error_output(
							ctx, "radar", code="RADAR_CONTINUATION_REJECTED", message=str(exc), recoverable=False,
							recovery_action="不要省略 --continue-run-id 重启旧任务；请在新的明确观察窗口重新运行。",
						)
						return
				elif not fresh and not adopt_visible_pages and not continue_visible_run_id:
					blocker = store.default_continuation_blocker(profile.fingerprint)
					if blocker:
						handle_error_output(
							ctx, "radar", code="RADAR_CONTINUATION_REJECTED", message=blocker, recoverable=False,
							recovery_action="先解决浏览器/平台环境问题；确认要放弃该中断检查点时，显式运行 boss radar run --fresh --profile <配置>。",
						)
						return
				# 岗位雷达只允许复用用户的正常 Chrome 会话（Bridge/CDP）。
				# 无头浏览器曾触发环境异常，不能在每日批量扫描中静默回退。
				with get_platform_instance(ctx, auth, browser_source="existing-browser") as platform:
					if fresh and not store.has_completed_baseline(profile.fingerprint):
						store.clear_baseline_progress(profile.fingerprint)
					result = RadarScanner(
                                           BossRadarSource(
                                                   platform,
                                                   transport=profile.boss_search_transport,
                                                   page_layout=profile.boss_page_layout,
                                           ), store, profile
					).run(
						max_requests=max_requests,
						resume=not fresh and not adopt_visible_pages and not continue_visible_run_id,
						continue_run_id=continue_run_id,
						visible_continuation=visible_continuation_context,
						adopt_visible_pages=visible_batches,
						reuse_visible_first_page=reuse_visible_first_page,
					)
					if continue_run_id:
						# This is a rolling logical scan, not a claim about either calendar day.
						result.today_events = store.list_events_for_runs(result.observation_run_ids)
					elif result.mode == "daily":
						local_now = datetime.now(_CHINA_TZ)
						day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
						result.today_events = store.list_new_events_between(
							profile_fingerprint=profile.fingerprint,
							start=day_start.astimezone(timezone.utc),
							end=(day_start + timedelta(days=1)).astimezone(timezone.utc),
						)
			data = result.to_dict()
			if collect_only:
				data["collection_only"] = {
					"enabled": True,
					"database_persisted": True,
					"xlsx_generated": False,
					"verification_performed": False,
					"note": (
						"本次仅保存采集数据库和运行证据；"
						"未生成旧版 Excel，未执行基于 Excel 的自动验收。"
					),
				}
				data["profile_path"] = str(profile_path(data_dir, profile_name).resolve())
				data["output_scope"] = (
					"temporary_page_override" if max_pages is not None else "configured_profile"
				)
				handle_output(
					ctx,
					"radar",
					data,
					render=_render_radar,
					hints={
						"next_actions": [
							"本次为 collect-only；请使用专用交付流程生成并核验最终工作簿。"
						],
					},
				)
				return
			exports = export_radar_workbook(result, resolve_output_dir(data_dir, profile))
			if result.complete:
				try:
					verification_report = verify_latest_radar_run(
						database_path=data_dir / "radar" / f"{profile.name}_radar.db",
						workbook_path=exports.latest,
						profile=profile,
						expected_mode=result.mode,
						output_scope="temporary_page_override" if max_pages is not None else "configured_profile",
					)
				except (
					RadarAcceptanceError,
					OSError,
					ValueError,
					sqlite3.Error,
					BadZipFile,
					InvalidFileException,
				) as exc:
					verification_error = str(exc)
	except RadarSafetyError as exc:
		handle_error_output(
			ctx, "radar", code="RADAR_SAFETY_STATE_ERROR",
			message=str(exc), recoverable=False,
			recovery_action="安全状态无法可靠读取，停止采集并检查本地历史；不要删除记录来解除阻止。",
		)
		return
	except RadarRunInProgressError as exc:
		handle_error_output(
			ctx,
			"radar",
			code="RADAR_ALREADY_RUNNING",
			message=str(exc),
			recoverable=True,
			recovery_action="等待当前岗位雷达完成；不要同时启动第二次扫描",
		)
		return
	except RadarSourceError as exc:
		handle_error_output(
			ctx,
			"radar",
			code=exc.code,
			message=exc.message,
			recoverable=False,
		)
		return
	except RadarExportError as exc:
		saved_note = (
			f"；本次扫描结果已完整保存到 {exc.saved_workbook.resolve()}"
			if exc.saved_workbook is not None
			else ""
		)
		recovery_action = (
			"直接打开上面的本次运行留档；关闭正在占用的 Excel 后等待下一次正常扫描即可，无需重新扫描本次岗位"
			if exc.saved_workbook is not None
			else "确认输出目录可写并关闭正在占用的 Excel 后再运行；本次没有生成可用留档"
		)
		handle_error_output(
			ctx,
			"radar",
			code="EXPORT_FAILED",
			message=f"{exc}{saved_note}",
			recoverable=True,
			recovery_action=recovery_action,
		)
		return
	if verification_error is not None:
		handle_error_output(
			ctx,
			"radar",
			code="VERIFY_FAILED",
			message=f"扫描和导出已完成，但自动验收无法执行：{verification_error}",
			recoverable=True,
			recovery_action=(
				f"不要重新扫描；本次运行留档位于 {exports.run_archive.resolve()}。"
				f"保留数据库与该文件，排查验收错误后运行 {verify_command}"
			),
		)
		return
	if verification_report is not None and not verification_report["passed"]:
		handle_error_output(
			ctx,
			"radar",
			code="VERIFY_FAILED",
			message="扫描和导出已完成，但数据库与 Excel 自动验收未通过",
			recoverable=True,
			recovery_action=(
				f"不要重新扫描；本次运行留档位于 {exports.run_archive.resolve()}。"
				"根据 failed_checks 修复本地导出或验收问题"
			),
			details={
				"run_id": verification_report["run_id"],
				"workbook": verification_report["workbook"],
				"failed_checks": verification_report["failed_checks"],
				"checks": verification_report["checks"],
			},
		)
		return
	data["profile_path"] = str(profile_path(data_dir, profile_name).resolve())
	data["exports"] = exports.to_dict()
	data["output_scope"] = "temporary_page_override" if max_pages is not None else "configured_profile"
	data["verification_command"] = verify_command
	data["verification"] = (
		{
			"performed": True,
			"passed": True,
			"checks_passed": verification_report["checks_passed"],
			"checks_total": verification_report["checks_total"],
			"failed_checks": [],
			"database_sha256": verification_report["database_sha256"],
			"workbook_sha256": verification_report["workbook_sha256"],
		}
		if verification_report is not None
		else {
			"performed": False,
			"passed": None,
			"reason": "coverage_incomplete",
			"note": "已保存不完整扫描审计文件；完成全部配置城市后才执行自动验收。",
		}
	)
	data["ready_for_full_coverage"] = bool(
		result.platform_results_exhausted
		and verification_report is not None
		and verification_report["passed"]
	)
	if not result.complete:
		resume_command = f"boss radar run --profile {profile.name}"
		if max_pages is not None:
			resume_command += f" --max-pages {max_pages}"
		if max_requests is not None:
			resume_command += f" --max-requests {max_requests}"
		recoverable, recovery_action = _incomplete_scan_recovery(
			stop_code=result.stop_code,
			handoff=result.operator_handoff,
			request_budget_exhausted=result.requests >= (max_requests or profile.max_requests),
			resume_command=resume_command,
		)
		handle_error_output(
			ctx,
			"radar",
			code=result.stop_code or "COVERAGE_INCOMPLETE",
			message=f"本次采集未完成：{result.stop_reason or '未覆盖全部配置城市和搜索词组合'}",
			recoverable=recoverable,
			recovery_action=recovery_action,
			details={
				"run_id": result.run_id,
				"archive_xlsx": str(exports.run_archive.resolve()),
				"coverage": data["coverage"],
			},
			data=data,
		)
		return
	handle_output(
		ctx,
		"radar",
		data,
		render=_render_radar,
		hints={
			"next_actions": [
				f"打开 {exports.latest.resolve()} 查看结果",
				(
					"下次运行同一命令即可只查看新增岗位"
					if verification_report is not None
					else "本次覆盖不完整；下次运行会续扫未完成城市"
				),
			],
		},
	)
