from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from boss_agent_cli.api.client import AccountRiskError, AuthError
from boss_agent_cli.api.browser_client import AttachedBrowserRequired, BridgeAccountRiskError
from boss_agent_cli.api.browser_source import BrowserSourceUnavailable
from boss_agent_cli.api.risk_diagnostics import extract_risk_diagnostics, safe_platform_message
from boss_agent_cli.auth.manager import AuthRequired, TokenRefreshFailed
from boss_agent_cli.radar.models import JobObservation, RadarEvent, is_relevant
from boss_agent_cli.radar.profile import RadarCity, RadarProfile
from boss_agent_cli.radar.store import RadarStore


_RETRYABLE_SOURCE_CODES = {"NETWORK_ERROR", "REQUEST_TIMEOUT", "TIMEOUT", "CITY_SCOPE_MISMATCH"}
_BOSS_SEARCH_TRANSPORTS = frozenset({"native_joblist", "page_capture"})
_CHINA_TZ = timezone(timedelta(hours=8))
_PAGE_VISIBILITY_REFUSAL = "Visible page is not currently visible; refusing any load-more action"
_PAGE_VISIBILITY_POLL_SECONDS = 15.0
# The extension rejects an invisible page before issuing a BOSS load-more action.
# Retain that no-action guard, but keep a baseline alive across an ordinary work
# session or overnight idle period instead of treating it as an environment error.
_PAGE_VISIBILITY_TIMEOUT_SECONDS = 8 * 60 * 60.0
_PAGE_VISIBILITY_TIMEOUT_LABEL = "8 小时"


class RadarSourceError(RuntimeError):
	def __init__(self, code: str, message: str, *, operator_handoff: dict[str, Any] | None = None) -> None:
		super().__init__(message)
		self.code = code
		self.message = message
		self.operator_handoff = operator_handoff


@dataclass(frozen=True)
class RadarSearchPage:
	items: tuple[dict[str, Any], ...]
	has_more: bool
	terminal_evidence: bool = False
	# ``page_capture`` supplies these from the settled, visible BOSS component.
	# They are optional so established non-browser sources remain compatible.
	page_size: int | None = None
	same_page_state: bool = False


class RadarSearchSource(Protocol):
	def search_page(self, query: str, city: RadarCity, page: int) -> RadarSearchPage: ...


class BossRadarSource:
	"""Thin adapter over the repository's existing authenticated BOSS platform."""

	def __init__(
		self,
		platform: Any,
		*,
		transport: str = "native_joblist",
		page_layout: str = "current_city",
	) -> None:
		if transport not in _BOSS_SEARCH_TRANSPORTS:
			raise ValueError(f"unsupported BOSS radar transport: {transport}")
		if page_layout not in {"current_city", "legacy_spa"}:
			raise ValueError(f"unsupported BOSS radar page layout: {page_layout}")
		self._platform = platform
		self.transport = transport
		self.page_layout = page_layout

	def search_page(self, query: str, city: RadarCity, page: int) -> RadarSearchPage:
		return self._search_page(query, city, page, reuse_visible_first_page=False)

	def search_page_with_options(
		self,
		query: str,
		city: RadarCity,
		page: int,
		*,
		reuse_visible_first_page: bool = False,
	) -> RadarSearchPage:
		return self._search_page(query, city, page, reuse_visible_first_page=reuse_visible_first_page)

	def _search_page(
		self,
		query: str,
		city: RadarCity,
		page: int,
		*,
		reuse_visible_first_page: bool,
	) -> RadarSearchPage:
		try:
			# Deep-linked search routes can redirect back to the homepage. This native
			# job-list channel has worked for small batches, but a full run has also
			# stopped with code 37. Keep one channel per scan so a failure never causes
			# an implicit fallback, repeated navigation, or a different session state.
			if self.transport == "page_capture":
				page_capture = getattr(self._platform, "search_jobs_page", None)
				if not callable(page_capture):
					raise RadarSourceError(
						"NOT_SUPPORTED",
						"当前 BOSS 浏览器通道不支持 page_capture；不会回退到 native_joblist",
					)
				# Retain the legacy call shape for current-city
				# capture so existing platform adapters/mocks do
				# not need a new optional keyword. The legacy SPA
				# route is opt-in and therefore receives its
				# explicit layout marker on every page.
				if self.page_layout == "legacy_spa":
					raw = page_capture(
						query, city.code, page,
						reuse_visible_first_page=reuse_visible_first_page,
						page_layout="legacy_spa",
					)
				elif reuse_visible_first_page:
					raw = page_capture(query, city.code, page, reuse_visible_first_page=True)
				else:
					raw = page_capture(query, city.code, page)
			else:
				raw = self._platform.search_jobs(
					query,
					page=page,
					raw_params={"city": city.code},
				)
		except RadarSourceError:
			raise
		except AccountRiskError as exc:
			raise RadarSourceError(
				"ACCOUNT_RISK",
				str(exc) or "平台触发账号风控",
				operator_handoff=_handoff_context(
					exc.diagnostics, city=city, query=query, page=page, transport=self.transport
				),
			) from exc
		except BridgeAccountRiskError as exc:
			raise RadarSourceError("ACCOUNT_RISK", str(exc) or "BOSS 页面要求安全验证") from exc
		except (AttachedBrowserRequired, BrowserSourceUnavailable) as exc:
			raise RadarSourceError("BROWSER_SESSION_REQUIRED", str(exc)) from exc
		except AuthRequired as exc:
			raise RadarSourceError("AUTH_REQUIRED", str(exc) or "未登录") from exc
		except TokenRefreshFailed as exc:
			raise RadarSourceError("TOKEN_REFRESH_FAILED", str(exc) or "登录态刷新失败") from exc
		except AuthError as exc:
			raise RadarSourceError("AUTH_EXPIRED", str(exc) or "登录态已失效") from exc
		except Exception as exc:
			message = str(exc).strip() or type(exc).__name__
			if self.transport == "page_capture" and message.endswith(_PAGE_VISIBILITY_REFUSAL):
				raise RadarSourceError("PAGE_NOT_VISIBLE", message) from exc
			raise RadarSourceError("BROWSER_ERROR", message) from exc
		# A successful envelope alone is not enough to establish a natural end.
		# In particular, a missing ``hasMore`` must never be coerced to False,
		# otherwise a malformed/intercepted response would look like the end of a
		# city scan and incorrectly unlock the next stage of the radar.
		if not isinstance(raw, dict):
			raise RadarSourceError("INVALID_RESPONSE", "平台返回的响应不是对象")
		if not self._platform.is_success(raw):
			code, _ = self._platform.parse_error(raw)
			if _is_account_risk_code(raw.get("code")):
				code = "ACCOUNT_RISK"
			raise RadarSourceError(
				code,
				safe_platform_message(raw, "岗位搜索失败"),
				operator_handoff=_operator_handoff(
					raw, city=city, query=query, page=page, transport=self.transport
				),
			)
		data = raw.get("zpData")
		if not isinstance(data, dict):
			raise RadarSourceError("INVALID_RESPONSE", "平台返回的成功响应缺少 zpData 对象")
		if "jobList" not in data or not isinstance(data["jobList"], list):
			raise RadarSourceError("INVALID_RESPONSE", "平台返回的成功响应缺少 jobList 列表")
		if "hasMore" not in data or type(data["hasMore"]) is not bool:
			raise RadarSourceError("INVALID_RESPONSE", "平台返回的成功响应缺少布尔型 hasMore")
		raw_items = data["jobList"]
		items = tuple(item for item in raw_items if isinstance(item, dict))
		labeled_cities = [str(item.get("cityName") or "").strip() for item in items]
		labeled_cities = [name for name in labeled_cities if name]
		if labeled_cities:
			matching = sum(_normalize_city_name(name) == _normalize_city_name(city.name) for name in labeled_cities)
			if matching / len(labeled_cities) < 0.8:
				raise RadarSourceError(
					"CITY_SCOPE_MISMATCH",
					f"{city.name} 搜索返回的岗位城市不匹配（{matching}/{len(labeled_cities)}）",
				)
		page_state = data.get("pageState")
		same_page_state = bool(
			self.transport == "page_capture"
			and isinstance(page_state, dict)
			and page_state.get("identityMatched") is True
			and page_state.get("hasMore") is data["hasMore"]
			and type(page_state.get("currentPage")) is int
			and page_state["currentPage"] == page
			and (data["hasMore"] is True or page_state.get("terminal") is True)
		)
		raw_page_size = page_state.get("pageSize") if isinstance(page_state, dict) else None
		page_size = (
			raw_page_size
			if type(raw_page_size) is int and 1 <= raw_page_size <= 100
			else None
		)
		# ``hasMore=false`` only describes the currently rendered
		# window.  The page-capture bridge separately reports whether
		# the settled result component regards that window as its real
		# terminal state.  Do not turn a capped or otherwise
		# non-continuable window into a completed city/query merely
		# because no loader is currently available.
		terminal_evidence = bool(
			same_page_state
			and data["hasMore"] is False
			and page_state.get("terminal") is True
		)
		return RadarSearchPage(
			items=items,
			has_more=data["hasMore"],
			terminal_evidence=terminal_evidence,
			page_size=page_size,
			same_page_state=same_page_state,
		)

	def verify_empty_terminal_auth(self, city: RadarCity, query: str, page: int) -> None:
		"""Do not treat an empty terminal page as exhaustion before auth is checked.

		Only the BOSS adapter exposes ``browser_user_info``. Other RadarSearchSource
		implementations deliberately retain their existing generic paging contract.
		"""
		check_auth = getattr(self._platform, "browser_user_info", None)
		if not callable(check_auth):
			return
		try:
			auth_response = check_auth()
		except AccountRiskError as exc:
			raise RadarSourceError(
				"ACCOUNT_RISK",
				str(exc) or "平台触发账号风控",
				operator_handoff=_handoff_context(
					exc.diagnostics, city=city, query=query, page=page, transport=self.transport
				),
			) from exc
		except BridgeAccountRiskError as exc:
			raise RadarSourceError("ACCOUNT_RISK", str(exc) or "BOSS 页面要求安全验证") from exc
		except AuthRequired as exc:
			raise RadarSourceError("AUTH_REQUIRED", str(exc) or "未登录") from exc
		except TokenRefreshFailed as exc:
			raise RadarSourceError("TOKEN_REFRESH_FAILED", str(exc) or "登录态刷新失败") from exc
		except AuthError as exc:
			raise RadarSourceError("AUTH_EXPIRED", str(exc) or "登录态已失效") from exc
		except Exception as exc:
			message = str(exc).strip() or type(exc).__name__
			raise RadarSourceError("BROWSER_ERROR", message) from exc

		if not isinstance(auth_response, dict):
			raise RadarSourceError("INVALID_RESPONSE", "浏览器登录态校验响应不是对象")
		if self._platform.is_success(auth_response):
			auth_data = auth_response.get("zpData")
			if not isinstance(auth_data, dict) or not auth_data:
				raise RadarSourceError("INVALID_RESPONSE", "浏览器登录态校验成功响应缺少非空 zpData 对象")
			return
		raw_code = auth_response.get("code")
		if raw_code in (7, "7"):
			raise RadarSourceError("AUTH_EXPIRED", safe_platform_message(auth_response, "浏览器登录状态已失效"))
		if _is_account_risk_code(raw_code):
			raise RadarSourceError(
				"ACCOUNT_RISK",
				safe_platform_message(auth_response, "浏览器登录态校验触发安全验证"),
				operator_handoff=_operator_handoff(
					auth_response, city=city, query=query, page=page, transport=self.transport
				),
			)
		code, _ = self._platform.parse_error(auth_response)
		raise RadarSourceError(code, safe_platform_message(auth_response, "浏览器登录态校验失败"))

	def supports_empty_terminal_auth(self) -> bool:
		"""Whether this adapter can actually issue the browser-only auth check."""
		# page_capture requires static same-page terminal evidence and must never
		# send the retired browser_user_info/debugger probe after an empty page.
		return self.transport != "page_capture" and callable(getattr(self._platform, "browser_user_info", None))

	def requires_empty_terminal_evidence(self) -> bool:
		return self.transport == "page_capture"

	def requires_terminal_evidence(self) -> bool:
		"""Whether every ``hasMore=false`` page needs bridge terminal proof."""
		return self.transport == "page_capture"


@dataclass
class RadarScanResult:
	run_id: str
	mode: str
	profile: RadarProfile
	baseline_reason: str = "initial"
	logical_scan_id: str = ""
	events: list[RadarEvent] = field(default_factory=list)
	requests: int = 0
	pages: int = 0
	jobs_seen: int = 0
	relevant_seen: int = 0
	unique_seen: int = 0
	completed_searches: list[str] = field(default_factory=list)
	carried_completed_searches: list[str] = field(default_factory=list)
	page_cap_reached_searches: list[str] = field(default_factory=list)
	terminal_full_searches: list[str] = field(default_factory=list)
	terminal_evidence_missing_searches: list[str] = field(default_factory=list)
	truncated_searches: list[str] = field(default_factory=list)
	failed_searches: list[str] = field(default_factory=list)
	stop_code: str = ""
	stop_reason: str = ""
	query_metrics: dict[str, dict[str, int]] = field(default_factory=dict)
	elapsed_seconds: float = 0.0
	observed_jobs: list[JobObservation] = field(default_factory=list)
	today_events: list[RadarEvent] | None = None
	event_window: dict[str, Any] | None = None
	page_limits: dict[str, int] = field(default_factory=dict)
	search_page_limits: dict[str, int] = field(default_factory=dict)
	search_progress: dict[str, dict[str, int]] = field(default_factory=dict)
	operator_handoff: dict[str, Any] | None = None
	observation_run_ids: list[str] = field(default_factory=list)
	scan_totals: dict[str, Any] = field(default_factory=dict)
	read_only_reanchor: dict[str, Any] | None = None
	visible_continuation: dict[str, Any] | None = None
	page_visibility_pauses: list[dict[str, Any]] = field(default_factory=list)

	@property
	def new_events(self) -> list[RadarEvent]:
		return [event for event in self.events if event.event_type in {"new", "reposted"}]

	@property
	def reportable_new_events(self) -> list[RadarEvent]:
		"""Events shown to the user: current run by default, or today's aggregate."""
		return self.new_events if self.today_events is None else self.today_events

	@property
	def complete(self) -> bool:
		"""Compatibility flag: every configured search finished its observation window.

		It deliberately says nothing about whether BOSS has exposed every job in a
		city.  Keep the historic field for callers, and use
		``query_window_complete`` / ``page_end_reached_searches`` in new UI.
		"""
		planned = len(self.profile.cities) * len(self.profile.queries)
		return (
			len(self.completed_searches) == planned
			and not self.truncated_searches
			and not self.failed_searches
			and not self.stop_code
		)

	@property
	def query_window_complete(self) -> bool:
		"""Explicit, bounded name for :attr:`complete`."""
		return self.complete

	@property
	def page_end_reached_searches(self) -> list[str]:
		"""Searches whose current visible result list reported ``hasMore=False``.

		A page end is useful evidence for resuming safely, but not proof that a
		city-wide inventory is exhaustive: BOSS can change, cap, or personalize the
		visible list.
		"""
		# A missing visible-state/terminal-size proof must not be promoted to a
		# page-end fact merely because the legacy completion path reached it.
		uncertain = set(self.page_cap_reached_searches) | set(self.terminal_evidence_missing_searches)
		return [key for key in self.completed_searches if key not in uncertain]

	@property
	def city_inventory_exhausted(self) -> bool:
		"""Whether this run proves every city inventory is exhaustive.

		The browser page-capture transport has no independent total/result-count
		evidence, so reaching its page end must never make this true.
		"""
		return False

	@property
	def platform_results_exhausted(self) -> bool:
		"""Legacy compatibility field; no current or retired channel proves city exhaustion."""
		return False

	@property
	def coverage_label(self) -> str:
		if not self.complete:
			return "配置搜索未完成"
		if self.page_cap_reached_searches:
			return "配置搜索已完成（部分到安全上限，页面仍有更多）"
		if self.terminal_full_searches:
			return "配置搜索已完成（终页满载，覆盖待核）"
		if self.terminal_evidence_missing_searches:
			return "配置搜索已完成（页面终点证据不足）"
		return "配置搜索已完成（页面到当前列表终点；未证明全城搜全）"

	@property
	def mode_label(self) -> str:
		if self.mode != "baseline":
			return "每日增量"
		return "配置变更基线" if self.baseline_reason == "profile_changed" else "首次基线"

	@property
	def successful_cities(self) -> list[str]:
		"""Cities whose configured query window completed (not inventory-complete)."""
		completed = set(self.completed_searches)
		return [
			city.name
			for city in self.profile.cities
			if all(_search_key(city, query) in completed for query in self.profile.queries)
		]

	@property
	def failed_cities(self) -> list[str]:
		"""Cities with a technical stop or an uncompleted configured search."""
		problem_searches = [*self.truncated_searches, *self.failed_searches]
		return [
			city.name
			for city in self.profile.cities
			if any(item.startswith(f"{city.name} / ") for item in problem_searches)
		]

	@property
	def coverage_review_cities(self) -> list[str]:
		"""Cities whose completed window still has an inventory-coverage caveat."""
		uncertain = {
			*self.page_cap_reached_searches,
			*self.terminal_full_searches,
			*self.terminal_evidence_missing_searches,
		}
		return [
			city.name
			for city in self.profile.cities
			if any(item.startswith(f"{city.name} / ") for item in uncertain)
		]

	def to_dict(self) -> dict[str, Any]:
		counts: dict[str, int] = {}
		for event in self.events:
			counts[event.event_type] = counts.get(event.event_type, 0) + 1
		today_counts: dict[str, int] = {}
		for event in self.reportable_new_events:
			today_counts[event.event_type] = today_counts.get(event.event_type, 0) + 1
		totals = self.scan_totals or {
			"segment_count": 1,
			"requests": self.requests,
			"pages": self.pages,
			"jobs_seen": self.jobs_seen,
			"relevant_seen": self.relevant_seen,
			"unique_seen": self.unique_seen,
			"elapsed_seconds": round(self.elapsed_seconds, 2),
			"query_metrics": self.query_metrics,
			"event_counts": counts,
		}
		return {
			"run_id": self.run_id,
			"logical_scan_id": self.logical_scan_id or self.run_id,
			"mode": self.mode,
			"mode_label": self.mode_label,
			"baseline_reason": self.baseline_reason if self.mode == "baseline" else None,
			"profile": self.profile.name,
			"boss_search_transport": self.profile.boss_search_transport,
			"coverage": {
				"scope": "精选城市",
				"boss_search_transport": self.profile.boss_search_transport,
				"cities": [city.name for city in self.profile.cities],
				"city_count": len(self.profile.cities),
				"successful_cities": self.successful_cities,
				"failed_cities": self.failed_cities,
				"queries": list(self.profile.queries),
				"planned_searches": len(self.profile.cities) * len(self.profile.queries),
				"city_page_limits": self.page_limits or self.profile.city_page_limits,
				"planned_page_requests": sum(
					(self.search_page_limits or self.profile.search_page_limits).values()
				),
				"search_page_limits": self.search_page_limits or self.profile.search_page_limits,
				"completed_searches": len(self.completed_searches),
				"resumed_completed_searches": len(self.carried_completed_searches),
				"page_cap_reached_searches": self.page_cap_reached_searches,
				"terminal_full_searches": self.terminal_full_searches,
				"terminal_evidence_missing_searches": self.terminal_evidence_missing_searches,
				"resume_from_pages": {
					key: progress.get("next_page", 1)
					for key, progress in self.search_progress.items()
				},
				"requests": self.requests,
				"pages": self.pages,
				"jobs_seen": self.jobs_seen,
				"relevant_seen": self.relevant_seen,
				"unique_seen": self.unique_seen,
				"elapsed_seconds": round(self.elapsed_seconds, 2),
				"complete": self.complete,
				"query_window_complete": self.query_window_complete,
				"page_end_reached_searches": self.page_end_reached_searches,
				"city_inventory_exhausted": self.city_inventory_exhausted,
				"city_inventory_exhaustion_status": "未证明",
				"coverage_review_cities": self.coverage_review_cities,
				"coverage_status": self.coverage_label,
				"platform_results_exhausted": self.platform_results_exhausted,
				"truncated_searches": self.truncated_searches,
				"failed_searches": self.failed_searches,
				"stop_code": self.stop_code or None,
				"stop_reason": self.stop_reason or None,
                                "operator_handoff": self.operator_handoff,
                                "page_visibility_pauses": self.page_visibility_pauses,
			},
			"query_metrics": self.query_metrics,
			"logical_scan": totals,
			"event_counts": counts,
			"today_event_counts": today_counts,
			"event_window": self.event_window,
			"new_jobs": [event.to_dict() for event in self.reportable_new_events],
			"note": self._note(),
		}

	def _note(self) -> str:
		window_note = "本结果按同一有界滚动扫描窗口汇总，岗位首次发现时间保留实际观察时刻，不等同于‘今日新发布’。 " if self.event_window else ""
		cap_note = (
			f" {len(self.page_cap_reached_searches)} 个搜索组合在安全上限后仍显示更多；本次结果按已扫描范围整理。"
			if self.page_cap_reached_searches
			else ""
		)
		terminal_note = (
			f" {len(self.terminal_full_searches)} 个搜索组合在满页时显示无更多；不能据此宣称已穷尽平台结果。"
			if self.terminal_full_searches
			else ""
		)
		terminal_evidence_note = (
			f" {len(self.terminal_evidence_missing_searches)} 个搜索组合缺少终页容量或同页状态证据；不能据此宣称已穷尽平台结果。"
			if self.terminal_evidence_missing_searches
			else ""
		)
		if self.mode == "baseline" and self.baseline_reason == "profile_changed" and not self.complete:
			return window_note + "配置变化后的新基线覆盖不完整；原历史仍保留，但尚不能按新窗口判断全部配置城市岗位。"
		if self.mode == "baseline" and not self.complete:
			return window_note + "首次基线覆盖不完整，不能据此判断全部配置城市岗位，也不会解锁每日新增模式。"
		if self.mode == "baseline" and self.baseline_reason == "profile_changed":
			return window_note + "已完成配置变化后的新观察窗口基线；原历史保留，新扩展范围的存量不冒充今日新增。" + cap_note + terminal_note + terminal_evidence_note
		if self.mode == "baseline":
			return window_note + "首次运行已完成配置扫描窗口并建立基线，不把存量岗位当作今日新增。" + cap_note + terminal_note + terminal_evidence_note
		if not self.complete:
			return window_note + "本次覆盖不完整，不能据此判断全部配置城市今日无新增。"
		return window_note + "已完成配置搜索窗口；页面终点不证明全城岗位已搜全。无可靠发布时间时，新增表示本系统今日首次发现。" + cap_note + terminal_note + terminal_evidence_note


class RadarScanner:
	def __init__(
		self,
		source: RadarSearchSource,
		store: RadarStore,
		profile: RadarProfile,
		*,
		sleep: Callable[[float], None] = time.sleep,
		random_uniform: Callable[[float, float], float] = random.uniform,
		monotonic: Callable[[], float] = time.monotonic,
	) -> None:
		self._source = source
		self._store = store
		self._profile = profile
		self._sleep = sleep
		self._random_uniform = random_uniform
		self._monotonic = monotonic

	def run(
		self,
		*,
		max_pages_per_query: int | None = None,
		max_requests: int | None = None,
		resume: bool = True,
		continue_run_id: str | None = None,
		visible_continuation: dict[str, Any] | None = None,
		adopt_visible_pages: dict[str, tuple[tuple[int, RadarSearchPage], ...]] | None = None,
		reuse_visible_first_page: bool = False,
	) -> RadarScanResult:
		page_limit_override = max_pages_per_query
		request_limit = self._profile.max_requests if max_requests is None else max_requests
		if page_limit_override is not None and page_limit_override < 1:
			raise ValueError("max_pages_per_query must be positive")
		# A direct per-run override would flatten a mixed 50/3 observation plan
		# while retaining this profile's mixed fingerprint.  The CLI deliberately
		# calls ``with_uniform_page_limit`` before constructing the scanner, so its
		# effective profile gets a separate baseline.  Refuse only the ambiguous
		# library-level combination rather than silently persisting incompatible
		# stats under the configured mixed identity.
		if page_limit_override is not None and self._profile.query_page_limits:
			raise ValueError(
				"query_page_limits profile cannot use direct max_pages_per_query; "
				"call profile.with_uniform_page_limit(...) before creating RadarScanner"
			)
		if request_limit < 1:
			raise ValueError("max_requests must be positive")
		searches = [(city, query) for query in self._profile.queries for city in self._profile.cities]

		started = self._monotonic()
		has_completed_baseline = self._store.has_completed_baseline(self._profile.fingerprint)
		mode = "daily" if has_completed_baseline else "baseline"
		if visible_continuation and (resume or continue_run_id or adopt_visible_pages or reuse_visible_first_page):
			raise ValueError("可见页接续必须是显式独立路径，不能与 resume、continue、采纳或首屏复用组合")
		if adopt_visible_pages and (resume or continue_run_id or reuse_visible_first_page):
			raise ValueError("可见页采纳必须新建基线，且不能与 resume、continue 或首屏复用组合")
		if resume and not continue_run_id:
			blocker = self._store.default_continuation_blocker(self._profile.fingerprint)
			if blocker:
				raise ValueError(f"RADAR_CONTINUATION_REJECTED: {blocker}")
		baseline_reason = "initial"
		if mode == "baseline" and self._store.has_jobs() and not self._store.profile_runs(
			profile_fingerprint=self._profile.fingerprint
		):
			baseline_reason = "profile_changed"
		if adopt_visible_pages:
			if mode != "baseline" or len(adopt_visible_pages) != 1:
				raise ValueError("可见页采纳仅支持一个新基线搜索")
			adopt_key, adopted_batches = next(iter(adopt_visible_pages.items()))
			adopt_matches = [(city, query) for city, query in searches if _search_key(city, query) == adopt_key]
			if len(adopt_matches) != 1 or tuple(page for page, _ in adopted_batches) != (1, 2):
				raise ValueError("可见页采纳批次必须是当前配置唯一搜索的连续第 1、2 页")
			first_page, second_page = (batch for _, batch in adopted_batches)
			page_size = first_page.page_size
			if (
				not first_page.same_page_state
				or not second_page.same_page_state
				or type(page_size) is not int
				or not 1 <= page_size <= 100
				or second_page.page_size != page_size
				or type(first_page.has_more) is not bool
				or type(second_page.has_more) is not bool
				or not first_page.has_more
				or len(first_page.items) != page_size
				or not second_page.items
				or len(second_page.items) > page_size
				or (second_page.has_more and len(second_page.items) != page_size)
				or (not second_page.has_more and not second_page.terminal_evidence)
			):
				raise ValueError("可见页采纳批次不满足完整第 1 页与第 2 页终点证据契约")
		else:
			adopt_key = ""
			adopt_matches = []
			adopted_batches = ()
		if visible_continuation:
			if mode != "baseline":
				raise ValueError("可见页接续仅支持尚未完成的 baseline")
			carried_raw = visible_continuation.get("completed_searches")
			progress_raw = visible_continuation.get("search_progress")
			logical_raw = visible_continuation.get("logical_scan_id")
			if not isinstance(carried_raw, set) or not isinstance(progress_raw, dict) or not isinstance(logical_raw, str) or not logical_raw:
				raise ValueError("可见页接续上下文无效")
			carried = set(carried_raw)
			resumed_logical_scan_id = logical_raw
			resumed_search_progress = {key: dict(value) for key, value in progress_raw.items() if isinstance(key, str) and isinstance(value, dict)}
			if len(resumed_search_progress) != 1:
				raise ValueError("可见页接续必须只有一个未完成搜索")
			baseline_reason = str(visible_continuation.get("baseline_reason") or baseline_reason)
			carried_page_caps = set()
			adopted_loaded_page = visible_continuation.get("adopted_loaded_page")
			if adopted_loaded_page is not None:
				if (
					not isinstance(adopted_loaded_page, tuple)
					or len(adopted_loaded_page) != 2
					or type(adopted_loaded_page[0]) is not int
					or not isinstance(adopted_loaded_page[1], RadarSearchPage)
				):
					raise ValueError("可见页接续的已加载页面证据无效")
			else:
				adopted_loaded_page = None
		elif continue_run_id:
			if mode != "daily" or not resume:
				raise ValueError("显式跨日续跑仅支持已有基线的 daily 观察窗口")
			carried, resumed_logical_scan_id, _logical_started_at = self._store.explicit_daily_continuation_context(
				run_id=continue_run_id,
				profile_fingerprint=self._profile.fingerprint,
			)
			carried_page_caps = self._store.logical_scan_page_cap_searches(
				self._profile.fingerprint, resumed_logical_scan_id
			)
			adopted_loaded_page = None
		elif mode == "baseline" and resume:
			carried, resumed_logical_scan_id, resumed_baseline_reason = self._store.resumable_baseline_context(
				self._profile.fingerprint
			)
			if resumed_baseline_reason:
				baseline_reason = resumed_baseline_reason
			carried_page_caps = self._store.baseline_page_cap_searches(self._profile.fingerprint)
			adopted_loaded_page = None
		elif mode == "daily" and resume:
			carried, resumed_logical_scan_id = self._store.resumable_daily_context(self._profile.fingerprint)
			carried_page_caps = self._store.logical_scan_page_cap_searches(
				self._profile.fingerprint,
				resumed_logical_scan_id,
			)
			adopted_loaded_page = None
		else:
			carried = set()
			resumed_logical_scan_id = ""
			carried_page_caps = set()
			adopted_loaded_page = None
		run_id = self._store.begin_run(mode)
		logical_scan_id = resumed_logical_scan_id or run_id
		prior_run_ids = self._store.logical_scan_run_ids(
			self._profile.fingerprint,
			logical_scan_id,
			mode=mode,
		)
		carried_terminal_full = self._store.logical_scan_terminal_full_searches(
			self._profile.fingerprint,
			logical_scan_id,
			mode=mode,
		)
		carried_terminal_evidence_missing = self._store.logical_scan_terminal_evidence_missing_searches(
			self._profile.fingerprint,
			logical_scan_id,
			mode=mode,
		)
		if visible_continuation is None:
			resumed_search_progress = self._store.logical_scan_search_progress(
				self._profile.fingerprint,
				logical_scan_id,
				mode=mode,
			)
		# 主搜索词优先覆盖所有城市。平台安全停止时，仍能最大化跨城市覆盖。
		first_configured_search = searches[0] if searches else None
		carried_in_order = [
			_search_key(city, query) for city, query in searches if _search_key(city, query) in carried
		]
		result = RadarScanResult(
			run_id=run_id,
			mode=mode,
			profile=self._profile,
			baseline_reason=baseline_reason,
			logical_scan_id=logical_scan_id,
			observation_run_ids=[*prior_run_ids, run_id],
			completed_searches=list(carried_in_order),
			carried_completed_searches=list(carried_in_order),
			page_cap_reached_searches=[key for key in carried_in_order if key in carried_page_caps],
			terminal_full_searches=[key for key in carried_in_order if key in carried_terminal_full],
			terminal_evidence_missing_searches=[
				key for key in carried_in_order if key in carried_terminal_evidence_missing
			],
			query_metrics={
				query: {
					"jobs_seen": 0,
					"relevant_seen": 0,
					"unique_contributed": 0,
					"not_in_primary_query": 0,
				}
				for query in self._profile.queries
			},
			page_limits={
				city.name: page_limit_override or self._profile.page_limit_for_city(city)
				for city in self._profile.cities
			},
			search_page_limits={
				_search_key(city, query): page_limit_override or self._profile.page_limit_for_city(city, query)
				for query in self._profile.queries
				for city in self._profile.cities
			},
			search_progress=resumed_search_progress,
			read_only_reanchor=(
				{
					"kind": "visible_loaded_pages",
					"search_key": adopt_key,
					"adopted_page_count": len(adopted_batches),
					"platform_requests": 0,
				}
				if adopted_batches else None
			),
			visible_continuation=(
				{**visible_continuation["metadata"], "recovered_run_id": run_id}
				if isinstance(visible_continuation, dict) and isinstance(visible_continuation.get("metadata"), dict)
				else None
			),
		)
		self._store.update_running_run(result.run_id, self._stats(result))
		identity_queries: dict[str, set[str]] = {}
		pending_observations: list[JobObservation] = []

		def persist_pending_observations() -> None:
			if not pending_observations:
				return
			result.events.extend(
					self._store.record_observations(
							result.run_id,
							result.mode,
							pending_observations,
							relevance_mode=self._profile.relevance_mode,
					)
			)
			pending_observations.clear()

		def persist_page_progress() -> None:
			"""Checkpoint each successful page before another platform request."""
			persist_pending_observations()
			result.unique_seen = len(identity_queries)
			result.elapsed_seconds = max(0.0, self._monotonic() - started)
			self._store.update_running_run(result.run_id, self._stats(result))

		pending_searches: list[tuple[RadarCity, str]] = []
		active_search_index: int | None = None
		try:
			if adopted_loaded_page is not None:
				loaded_page_number, loaded_page = adopted_loaded_page
				loaded_key = next(iter(resumed_search_progress), "")
				loaded_parts = loaded_key.split(" / ", 1)
				loaded_city = next(
						(city for city in self._profile.cities if city.name == loaded_parts[0]), None
				) if len(loaded_parts) == 2 else None
				if loaded_city is None or len(loaded_parts) != 2:
					raise ValueError("可见页接续的已加载页面缺少配置城市或关键词")
				self._adopt_visible_continuation_page(
						result,
						loaded_city,
						loaded_parts[1],
						loaded_page_number,
						loaded_page,
						identity_queries,
						pending_observations,
				)
				persist_page_progress()
			if adopted_batches:
				adopt_city, adopt_query = adopt_matches[0]
				self._adopt_visible_pages(
					result,
					adopt_city,
					adopt_query,
					adopted_batches,
					identity_queries,
					pending_observations,
				)
				persist_page_progress()
			pending_searches = [
				(city, query)
				for city, query in searches
				if _search_key(city, query) not in carried
				and _search_key(city, query) not in result.completed_searches
			]
			if adopted_batches:
				pending_searches.sort(key=lambda item: 0 if _search_key(*item) == adopt_key else 1)
			try:
				for index, (city, query) in enumerate(pending_searches):
					active_search_index = index
					if result.requests >= request_limit:
						result.truncated_searches.extend(
							_search_key(remaining_city, remaining_query)
							for remaining_city, remaining_query in pending_searches[index:]
						)
						break
					try:
						self._scan_search(
							result,
							city,
							query,
							identity_queries,
							pending_observations,
							persist_page_progress,
							max_pages=result.search_page_limits[_search_key(city, query)],
							max_requests=request_limit,
							reuse_visible_first_page=(
								reuse_visible_first_page and (city, query) == first_configured_search
							),
						)
						# Commit each completed city/query before moving on. A later
						# browser failure or user stop must not force safe, completed
						# work to be fetched from the platform again.
						persist_pending_observations()
						if mode == "baseline" and _search_key(city, query) in result.completed_searches:
							self._store.mark_baseline_searches(
								self._profile.fingerprint,
								(_search_key(city, query),),
							)
						result.unique_seen = len(identity_queries)
						result.elapsed_seconds = max(0.0, self._monotonic() - started)
						self._store.update_running_run(result.run_id, self._stats(result))
					except RadarSourceError as exc:
						result.stop_code = exc.code
						result.stop_reason = exc.message
						result.operator_handoff = exc.operator_handoff
						result.failed_searches.append(f"{_search_key(city, query)}: {exc.code} {exc.message}")
						result.truncated_searches.extend(
							_search_key(remaining_city, remaining_query)
							for remaining_city, remaining_query in pending_searches[index + 1 :]
						)
						break
				else:
					active_search_index = None
			except KeyboardInterrupt:
				result.stop_code = "USER_CANCELLED"
				result.stop_reason = "用户停止扫描；已保留停止前完成和读取到的岗位"
				if active_search_index is not None:
					result.truncated_searches.extend(
						_search_key(remaining_city, remaining_query)
						for remaining_city, remaining_query in pending_searches[active_search_index:]
						if _search_key(remaining_city, remaining_query) not in result.completed_searches
					)
			for queries in identity_queries.values():
				if len(queries) == 1:
					result.query_metrics[next(iter(queries))]["unique_contributed"] += 1
				if self._profile.queries[0] not in queries:
					for query in queries:
						result.query_metrics[query]["not_in_primary_query"] += 1
			persist_pending_observations()
			# Rehydrate user-facing rows from SQLite so "首次发现时间" comes from
			# the durable first_seen_at value instead of this run's observation time.
			result.observed_jobs = self._store.list_jobs_for_runs(
					result.observation_run_ids,
					relevance_mode=self._profile.relevance_mode,
			)
			if mode == "baseline":
				self._store.mark_baseline_searches(self._profile.fingerprint, result.completed_searches)
		except Exception:
			result.unique_seen = len(identity_queries)
			result.elapsed_seconds = max(0.0, self._monotonic() - started)
			self._store.finish_run(result.run_id, status="failed", stats=self._stats(result))
			raise

		result.completed_searches = list(dict.fromkeys(result.completed_searches))
		result.page_cap_reached_searches = list(dict.fromkeys(result.page_cap_reached_searches))
		result.terminal_full_searches = list(dict.fromkeys(result.terminal_full_searches))
		result.terminal_evidence_missing_searches = list(
			dict.fromkeys(result.terminal_evidence_missing_searches)
		)
		result.truncated_searches = list(dict.fromkeys(result.truncated_searches))
		result.failed_searches = list(dict.fromkeys(result.failed_searches))
		result.unique_seen = len(identity_queries)
		result.elapsed_seconds = max(0.0, self._monotonic() - started)
		result.event_window = self._event_window(result, force_rolling=bool(continue_run_id))
		if result.event_window is not None:
			result.today_events = self._store.list_events_for_runs(result.observation_run_ids)
		result.scan_totals = self._build_scan_totals(result, prior_run_ids)
		status = "completed" if result.complete else "stopped"
		self._store.finish_run(result.run_id, status=status, stats=self._stats(result))
		# ``finish_run`` supplies the authoritative terminal timestamp. Refresh and
		# persist once so a rolling-window verifier can compare exact segment bounds.
		if result.event_window is not None:
			result.event_window = self._event_window(result, force_rolling=True)
			self._store.finish_run(result.run_id, status=status, stats=self._stats(result))
		return result

	def _scan_search(
		self,
		result: RadarScanResult,
		city: RadarCity,
		query: str,
		identity_queries: dict[str, set[str]],
		pending_observations: list[JobObservation],
		persist_page_progress: Callable[[], None],
		*,
		max_pages: int,
		max_requests: int,
		reuse_visible_first_page: bool = False,
	) -> None:
		key = _search_key(city, query)
		progress = result.search_progress.get(key, {})
		page = max(1, _metric_int(progress.get("next_page")) or 1)
		search_rank = max(0, _metric_int(progress.get("source_rank")))
		while page <= max_pages and result.requests < max_requests:
			page_start_rank = search_rank
			search_page = self._fetch_page(
				result,
				city,
				query,
				page,
				max_requests=max_requests,
				reuse_visible_first_page=reuse_visible_first_page and page == 1,
			)
			if search_page is None:
				if result.requests >= max_requests:
					result.truncated_searches.append(key)
					return
				raise RadarSourceError("NETWORK_ERROR", "网络请求重试耗尽")
			if not search_page.items and not search_page.has_more:
				requires_terminal_evidence = getattr(self._source, "requires_empty_terminal_evidence", None)
				if (
					not (callable(requires_terminal_evidence) and requires_terminal_evidence())
					and not self._verify_empty_terminal_auth(result, city, query, page, key, max_requests=max_requests)
				):
					return
			self._store.record_page_capture(
				result.run_id,
				shard=city.name,
				query=query,
				page=page,
				raw_job_count=len(search_page.items),
				has_more=search_page.has_more,
				page_size=search_page.page_size,
				same_page_state=search_page.same_page_state,
			)
			result.pages += 1
			result.jobs_seen += len(search_page.items)
			metrics = result.query_metrics[query]
			metrics["jobs_seen"] += len(search_page.items)
			observations = [
				JobObservation.from_boss(
					item,
					query=query,
					shard=city.name,
					source_page=page,
					source_rank=search_rank + offset,
				)
				for offset, item in enumerate(search_page.items, start=1)
			]
			search_rank += len(search_page.items)
			relevant = [
				item for item in observations
				if is_relevant(
					item,
						relevance_mode=self._profile.relevance_mode,
					exclude_internships=self._profile.job_exclusions.exclude_internships,
					exclude_hourly_daily_pay=self._profile.job_exclusions.exclude_hourly_daily_pay,
				)
			]
			result.relevant_seen += len(relevant)
			metrics["relevant_seen"] += len(relevant)
			for observation in relevant:
				identity_queries.setdefault(observation.identity_key, set()).add(query)
			pending_observations.extend(relevant)
			if not search_page.has_more:
				requires_terminal_evidence = getattr(self._source, "requires_terminal_evidence", None)
				if (
					callable(requires_terminal_evidence)
					and requires_terminal_evidence()
					and not search_page.terminal_evidence
				):
					# ``hasMore=false`` without the bridge's explicit terminal
					# signal is an observation boundary, not a verified endpoint.
					# Re-read this page on a later resumable segment: it is the
					# only safe way to learn whether the page has become terminal
					# or exposes a continuation. Preserve the rank before this
					# page so a successful retry has a stable source ordering.
					result.terminal_evidence_missing_searches.append(key)
					result.truncated_searches.append(key)
					result.search_progress[key] = {
						"next_page": page,
						"source_rank": page_start_rank,
					}
					persist_page_progress()
					return
				if self._profile.boss_search_transport == "page_capture":
					if not search_page.same_page_state or search_page.page_size is None:
						result.terminal_evidence_missing_searches.append(key)
					elif len(search_page.items) >= search_page.page_size:
						result.terminal_full_searches.append(key)
				result.completed_searches.append(key)
				result.search_progress.pop(key, None)
				persist_page_progress()
				return
			result.search_progress[key] = {
				"next_page": page + 1,
				"source_rank": search_rank,
			}
			persist_page_progress()
			page += 1

		if page > max_pages:
			# max_pages_per_query defines the configured observation window. Reaching
			# that boundary after every requested page succeeded is not a failed scan,
			# but it must remain visible so nobody mistakes it for platform exhaustion.
			result.completed_searches.append(key)
			result.page_cap_reached_searches.append(key)
			result.search_progress.pop(key, None)
			persist_page_progress()
		else:
			result.truncated_searches.append(key)

	def _adopt_visible_pages(
                self,
                result: RadarScanResult,
		city: RadarCity,
		query: str,
		batches: tuple[tuple[int, RadarSearchPage], ...],
		identity_queries: dict[str, set[str]],
		pending_observations: list[JobObservation],
	) -> None:
		"""Persist already-rendered p1/p2 batches without a platform request."""
		key = _search_key(city, query)
		source_rank = 0
		for page, search_page in batches:
			self._store.record_page_capture(
				result.run_id,
				shard=city.name,
				query=query,
				page=page,
				raw_job_count=len(search_page.items),
				has_more=search_page.has_more,
				page_size=search_page.page_size,
				same_page_state=True,
			)
			result.pages += 1
			result.jobs_seen += len(search_page.items)
			metrics = result.query_metrics[query]
			metrics["jobs_seen"] += len(search_page.items)
			observations = [
				JobObservation.from_boss(
					item,
					query=query,
					shard=city.name,
					source_page=page,
					source_rank=source_rank + offset,
				)
				for offset, item in enumerate(search_page.items, start=1)
			]
			source_rank += len(search_page.items)
			relevant = [
				item for item in observations
				if is_relevant(
					item,
						relevance_mode=self._profile.relevance_mode,
					exclude_internships=self._profile.job_exclusions.exclude_internships,
					exclude_hourly_daily_pay=self._profile.job_exclusions.exclude_hourly_daily_pay,
				)
			]
			result.relevant_seen += len(relevant)
			metrics["relevant_seen"] += len(relevant)
			for observation in relevant:
				identity_queries.setdefault(observation.identity_key, set()).add(query)
			pending_observations.extend(relevant)
			last_page = batches[-1][1]
			if last_page.has_more:
				result.search_progress[key] = {"next_page": batches[-1][0] + 1, "source_rank": source_rank}
			else:
				# This uses the same terminal accounting as a normally captured
				# page. It is a new read-only baseline segment, not a recovery claim
				# about the earlier failed run.
				if len(last_page.items) >= (last_page.page_size or 0):
					result.terminal_full_searches.append(key)
				result.completed_searches.append(key)
				result.search_progress.pop(key, None)

	def _adopt_visible_continuation_page(
				self,
				result: RadarScanResult,
				city: RadarCity,
				query: str,
				page: int,
				search_page: RadarSearchPage,
				identity_queries: dict[str, set[str]],
				pending_observations: list[JobObservation],
		) -> None:
			"""Persist one page that visibly advanced after a prior timeout.

			The caller has already read this exact rendered suffix without a page
			action. It may only extend the sole stored continuation checkpoint by
			one full nonterminal page; it cannot create a new search, skip a page,
			or infer a terminal result.
			"""
			key = _search_key(city, query)
			progress = result.search_progress.get(key)
			if not isinstance(progress, dict):
				raise ValueError("可见页接续缺少待采纳页面的断点")
			expected_page = _metric_int(progress.get("next_page"))
			source_rank = _metric_int(progress.get("source_rank"))
			if (
				expected_page is None
				or source_rank is None
				or page != expected_page
				or search_page.has_more is not True
				or search_page.same_page_state is not True
				or search_page.page_size is None
				or len(search_page.items) != search_page.page_size
			):
				raise ValueError("可见页接续的已加载页面不是完整的下一非终页")
			self._store.record_page_capture(
					result.run_id,
					shard=city.name,
					query=query,
					page=page,
					raw_job_count=len(search_page.items),
					has_more=True,
					page_size=search_page.page_size,
					same_page_state=True,
			)
			result.pages += 1
			result.jobs_seen += len(search_page.items)
			metrics = result.query_metrics[query]
			metrics["jobs_seen"] += len(search_page.items)
			observations = [
					JobObservation.from_boss(
							item,
							query=query,
							shard=city.name,
							source_page=page,
							source_rank=source_rank + offset,
						)
					for offset, item in enumerate(search_page.items, start=1)
			]
			relevant = [
					item
					for item in observations
						if is_relevant(
							item,
								relevance_mode=self._profile.relevance_mode,
							exclude_internships=self._profile.job_exclusions.exclude_internships,
							exclude_hourly_daily_pay=self._profile.job_exclusions.exclude_hourly_daily_pay,
					)
			]
			result.relevant_seen += len(relevant)
			metrics["relevant_seen"] += len(relevant)
			for observation in relevant:
				identity_queries.setdefault(observation.identity_key, set()).add(query)
			pending_observations.extend(relevant)
			result.search_progress[key] = {
					"next_page": page + 1,
					"source_rank": source_rank + len(search_page.items),
			}

	def _verify_empty_terminal_auth(
		self,
		result: RadarScanResult,
		city: RadarCity,
		query: str,
		page: int,
		key: str,
		*,
		max_requests: int,
	) -> bool:
		"""Spend one bounded browser request before accepting an empty endpoint."""
		check_auth = getattr(self._source, "verify_empty_terminal_auth", None)
		if not callable(check_auth):
			return True
		supports_auth = getattr(self._source, "supports_empty_terminal_auth", None)
		if callable(supports_auth) and not supports_auth():
			return True
		if result.requests >= max_requests:
			result.truncated_searches.append(key)
			return False
		if result.requests:
			delay = self._random_uniform(*self._profile.delay_range)
			if delay:
				self._sleep(delay)
		result.requests += 1
		check_auth(city, query, page)
		return True

	def _fetch_page(
		self,
		result: RadarScanResult,
		city: RadarCity,
		query: str,
		page: int,
		*,
		max_requests: int,
		reuse_visible_first_page: bool = False,
	) -> RadarSearchPage | None:
		last_retryable_error: RadarSourceError | None = None
		attempt = 0
		visibility_wait_started: float | None = None
		visibility_wait_polls = 0
		while True:
			if result.requests >= max_requests:
				# ``None`` is reserved for a request budget that was already empty.
				# Do not disguise a real page failure merely because its final retry
				# happened to consume the last available request.
				if last_retryable_error is not None:
					raise last_retryable_error
				return None
			if result.requests and visibility_wait_started is None:
				delay = (
					self._profile.retry_delay_seconds if attempt else self._random_uniform(*self._profile.delay_range)
				)
				if delay:
					self._sleep(delay)
			result.requests += 1
			try:
				if reuse_visible_first_page:
					with_options = getattr(self._source, "search_page_with_options", None)
					if not callable(with_options):
						raise RadarSourceError("NOT_SUPPORTED", "当前页面采集通道不支持复用首屏")
					fetched = with_options(query, city, page, reuse_visible_first_page=True)
				else:
					fetched = self._source.search_page(query, city, page)
				if visibility_wait_started is not None:
					result.page_visibility_pauses.append({
						"city": city.name,
						"query": query,
						"page": page,
						"polls": visibility_wait_polls,
						"wait_seconds": round(max(0.0, self._monotonic() - visibility_wait_started), 2),
					})
				return fetched
			except RadarSourceError as exc:
				if exc.code == "PAGE_NOT_VISIBLE":
					# The extension refused before any BOSS loader action. Do not consume the
					# request budget, steal focus, refresh, or turn this into a platform retry.
					result.requests -= 1
					current = self._monotonic()
					if visibility_wait_started is None:
						visibility_wait_started = current
					if current - visibility_wait_started >= _PAGE_VISIBILITY_TIMEOUT_SECONDS:
						raise RadarSourceError(
							"PAGE_VISIBILITY_TIMEOUT",
                                                  f"页面持续不可见超过 {_PAGE_VISIBILITY_TIMEOUT_LABEL}；未发送加载动作",
						) from exc
					visibility_wait_polls += 1
					self._sleep(_PAGE_VISIBILITY_POLL_SECONDS)
					continue
				if exc.code not in _RETRYABLE_SOURCE_CODES:
					raise
				last_retryable_error = exc
				if attempt >= self._profile.max_retries:
					raise
				attempt += 1
		# Defensive only: the loop either returned a successful page, stopped
		# before a request, or re-raised the last source failure.
		if last_retryable_error is not None:  # pragma: no cover
			raise last_retryable_error
		return None  # pragma: no cover

	def _stats(self, result: RadarScanResult) -> dict[str, Any]:
		return {
			"profile_name": self._profile.name,
			"profile_fingerprint": self._profile.fingerprint,
			"relevance_mode": self._profile.relevance_mode,
			"output_scope": (
				"temporary_page_override" if self._profile.page_limit_override is not None else "configured_profile"
			),
			"page_limit_override": self._profile.page_limit_override,
			"boss_search_transport": self._profile.boss_search_transport,
			"baseline_reason": result.baseline_reason if result.mode == "baseline" else "",
			"logical_scan_id": result.logical_scan_id or result.run_id,
			"observation_run_ids": result.observation_run_ids or [result.run_id],
			"event_window": result.event_window,
			"complete": result.complete,
			"requests": result.requests,
			"pages": result.pages,
			"jobs_seen": result.jobs_seen,
			"relevant_seen": result.relevant_seen,
			"unique_seen": result.unique_seen,
			"elapsed_seconds": round(result.elapsed_seconds, 2),
			"completed_searches": result.completed_searches,
			"carried_completed_searches": result.carried_completed_searches,
			"page_cap_reached_searches": result.page_cap_reached_searches,
			"terminal_full_searches": result.terminal_full_searches,
			"terminal_evidence_missing_searches": result.terminal_evidence_missing_searches,
			"search_progress": result.search_progress,
			"truncated_searches": result.truncated_searches,
			"failed_searches": result.failed_searches,
			"stop_code": result.stop_code,
			"stop_reason": result.stop_reason,
			"operator_handoff": result.operator_handoff,
			"query_metrics": result.query_metrics,
			"scan_totals": result.scan_totals,
			"read_only_reanchor": result.read_only_reanchor,
				"visible_continuation": result.visible_continuation,
				"page_visibility_pauses": result.page_visibility_pauses,
				"city_page_limits": result.page_limits,
			"search_page_limits": result.search_page_limits,
		}

	def _event_window(self, result: RadarScanResult, *, force_rolling: bool) -> dict[str, Any] | None:
		segments = [self._store.run_summary(run_id) for run_id in result.observation_run_ids]
		try:
			starts = [datetime.fromisoformat(str(item["started_at"]).replace("Z", "+00:00")) for item in segments]
			ends = [
				datetime.now(_CHINA_TZ) if str(item["run_id"]) == result.run_id and not item["completed_at"]
				else datetime.fromisoformat(str(item["completed_at"] or item["started_at"]).replace("Z", "+00:00"))
				for item in segments
			]
		except (TypeError, ValueError):
			return None
		if any(value.tzinfo is None or value.utcoffset() is None for value in [*starts, *ends]):
			return None
		start, end = min(starts), max(ends)
		crosses_midnight = start.astimezone(_CHINA_TZ).date() != end.astimezone(_CHINA_TZ).date()
		if not force_rolling and not crosses_midnight:
			return None
		return {"mode": "rolling", "start_at": start.astimezone(_CHINA_TZ).isoformat(), "end_at": end.astimezone(_CHINA_TZ).isoformat(), "run_ids": result.observation_run_ids, "crosses_midnight": crosses_midnight}

	def _build_scan_totals(self, result: RadarScanResult, prior_run_ids: list[str]) -> dict[str, Any]:
		"""Aggregate user-facing metrics across every segment of one resumed scan."""
		prior_summaries = [self._store.run_summary(run_id) for run_id in prior_run_ids]
		prior_stats = [summary["stats"] for summary in prior_summaries]
		query_metrics = {
			query: {
				"jobs_seen": result.query_metrics.get(query, {}).get("jobs_seen", 0),
				"relevant_seen": result.query_metrics.get(query, {}).get("relevant_seen", 0),
				"unique_contributed": 0,
				"not_in_primary_query": 0,
			}
			for query in self._profile.queries
		}
		for stats in prior_stats:
			raw_metrics = stats.get("query_metrics")
			if not isinstance(raw_metrics, dict):
				continue
			for query in self._profile.queries:
				metrics = raw_metrics.get(query)
				if not isinstance(metrics, dict):
					continue
				query_metrics[query]["jobs_seen"] += _metric_int(metrics.get("jobs_seen"))
				query_metrics[query]["relevant_seen"] += _metric_int(metrics.get("relevant_seen"))

		overlap = self._store.query_overlap_metrics_for_runs(
			result.observation_run_ids,
			queries=self._profile.queries,
			primary_query=self._profile.queries[0],
		)
		for query, metrics in overlap.items():
			query_metrics[query]["unique_contributed"] = metrics["unique_contributed"]
			query_metrics[query]["not_in_primary_query"] = metrics["not_in_primary_query"]

		event_counts: dict[str, int] = {}
		for summary in prior_summaries:
			for event_type, count in summary["event_counts"].items():
				event_counts[event_type] = event_counts.get(event_type, 0) + int(count)
		for event in result.events:
			event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1

		return {
			"segment_count": len(result.observation_run_ids),
			"requests": result.requests + sum(_metric_int(stats.get("requests")) for stats in prior_stats),
			"pages": result.pages + sum(_metric_int(stats.get("pages")) for stats in prior_stats),
			"jobs_seen": result.jobs_seen + sum(_metric_int(stats.get("jobs_seen")) for stats in prior_stats),
			"relevant_seen": result.relevant_seen
			+ sum(_metric_int(stats.get("relevant_seen")) for stats in prior_stats),
			"unique_seen": len(result.observed_jobs),
			"elapsed_seconds": round(
				result.elapsed_seconds + sum(_metric_float(stats.get("elapsed_seconds")) for stats in prior_stats),
				2,
			),
			"query_metrics": query_metrics,
			"event_counts": event_counts,
		}


def _search_key(city: RadarCity, query: str) -> str:
	return f"{city.name} / {query}"


def _metric_int(value: Any) -> int:
	return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _metric_float(value: Any) -> float:
	return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _normalize_city_name(value: str) -> str:
	name = value.strip()
	for suffix in ("特别行政区", "自治州", "地区", "市"):
		if name.endswith(suffix):
			return name[: -len(suffix)]
	return name


def _operator_handoff(
	raw: dict[str, Any], *, city: RadarCity, query: str, page: int, transport: str
) -> dict[str, Any]:
	diagnostics = extract_risk_diagnostics(raw)
	if _is_account_risk_code(raw.get("code")):
		diagnostics["needs_browser_handoff"] = True
	return _handoff_context(diagnostics, city=city, query=query, page=page, transport=transport)


def _handoff_context(
	diagnostics: dict[str, Any], *, city: RadarCity, query: str, page: int, transport: str
) -> dict[str, Any]:
	"""Add scan coordinates to already-sanitized transport diagnostics."""
	return {
		**diagnostics,
		"channel": transport,
		"city": city.name,
		"query": query,
		"page": page,
	}


def _is_account_risk_code(value: Any) -> bool:
	"""Codes which must stop the scan regardless of a platform adapter mapping."""
	return value in (32, "32", 36, "36", 37, "37")
