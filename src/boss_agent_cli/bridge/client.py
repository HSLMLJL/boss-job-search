"""Browser Bridge 客户端 — CLI 侧调用 daemon 的 HTTP 接口。"""

import json
import time
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from boss_agent_cli.bridge.protocol import (
	BRIDGE_HOST, BRIDGE_PORT,
	DAEMON_PING_PATH, DAEMON_STATUS_PATH, DAEMON_COMMAND_PATH, DAEMON_RESULT_PATH,
	BridgeCommand, BridgeResult, is_radar_search_url, make_command_id, validate_visible_adoption,
)

SUPPORTED_EXTENSION_MAJOR_VERSION = "1"
DIAGNOSTIC_NAVIGATE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}{DAEMON_PING_PATH}"


class BridgeNotRunning(Exception):
	pass


class BridgeExtensionDisconnected(Exception):
	pass


class BridgeClient:
	"""与 Bridge daemon 通信的 HTTP 客户端。"""

	def __init__(self, *, timeout: float = 65.0, max_retries: int = 4, capture_recovery_timeout: float = 240.0):
		# Compatibility: despite its long-standing name, this is the total number
		# of command attempts (not the number of retries after an initial attempt).
		# Rejecting zero prevents a silent no-op that can look like a browser error.
		if type(max_retries) is not int or max_retries < 1:
			raise ValueError("max_retries must be a non-bool integer of at least 1")
		self._base_url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
		self._timeout = timeout
		self._max_retries = max_retries
		self._capture_recovery_timeout = max(0.0, min(float(capture_recovery_timeout), 240.0))

	def is_running(self) -> bool:
		"""检查 daemon 是否在运行。"""
		try:
			resp = httpx.get(
				f"{self._base_url}{DAEMON_PING_PATH}",
				timeout=2.0,
				trust_env=False,
			)
			return resp.status_code == 200
		except (httpx.HTTPError, OSError):
			return False

	def status(self) -> dict[str, Any] | None:
		"""获取 daemon 状态。"""
		try:
			resp = httpx.get(
				f"{self._base_url}{DAEMON_STATUS_PATH}",
				timeout=2.0,
				trust_env=False,
			)
			if resp.status_code == 200:
				data = resp.json()
				if isinstance(data, dict):
					return cast("dict[str, Any]", data)
			return None
		except (httpx.HTTPError, ValueError, OSError):
			return None

	def is_extension_connected(self) -> bool:
		"""检查扩展是否已连接。"""
		st = self.status()
		return st is not None and st.get("extensionConnected", False)

	def diagnose(
		self,
		*,
		workspace: str = "boss",
		run_probes: bool = True,
		run_navigation_probe: bool = False,
	) -> list[dict[str, Any]]:
		"""Return redacted local Bridge health checks for doctor output.

		The probes are local capability checks only. They do not read cookies,
		headers, platform pages, or account data. Navigation is opt-in because a
		diagnostic command must not replace an existing automation workspace tab.
		"""
		checks: list[dict[str, Any]] = []

		status = self.status()
		if status is None:
			checks.append(_bridge_check(
				"bridge_daemon",
				"warn",
				f"Bridge daemon 未运行或无法访问 http://{BRIDGE_HOST}:{BRIDGE_PORT}",
				"运行 boss doctor 前先启动 Bridge daemon，或检查 19826 端口是否被占用",
			))
			checks.append(_bridge_check(
				"bridge_extension",
				"warn",
				"Chrome 扩展尚未连接到本地 Bridge daemon",
				"安装并启用 extension/ 目录中的 BOSS Agent Bridge 扩展",
			))
			return checks

		pid = status.get("pid", "unknown")
		uptime = status.get("uptime", 0)
		checks.append(_bridge_check(
			"bridge_daemon",
			"ok",
			f"Bridge daemon 运行中 pid={pid} uptime={uptime}s",
			None,
			{"pid": pid, "uptime": uptime, "base_url": self._base_url},
		))

		extension_connected = bool(status.get("extensionConnected"))
		extension_version = str(status.get("extensionVersion") or "")
		checks.append(_bridge_check(
			"bridge_extension",
			"ok" if extension_connected else "warn",
			f"Chrome 扩展已连接 version={extension_version or 'unknown'}"
			if extension_connected else "Chrome 扩展未连接",
			None if extension_connected else "打开 chrome://extensions，安装并启用 extension/，再等待扩展连接 daemon",
			{"extension_version": extension_version or None},
		))

		if extension_connected:
			checks.append(_bridge_check(
				"bridge_protocol",
				"ok" if _is_supported_extension_version(extension_version) else "warn",
				f"扩展协议版本 {extension_version or 'unknown'}；CLI 期望 major={SUPPORTED_EXTENSION_MAJOR_VERSION}",
				None if _is_supported_extension_version(extension_version) else "重新加载与当前 CLI 匹配的 extension/ 扩展",
				{
					"extension_version": extension_version or None,
					"expected_major": SUPPORTED_EXTENSION_MAJOR_VERSION,
				},
			))
		else:
			checks.append(_bridge_check(
				"bridge_protocol",
				"warn",
				"扩展未连接，无法确认协议版本",
				"先连接扩展，再重新运行 boss doctor",
			))

		if not extension_connected or not run_probes:
			return checks

		exec_result = self.send_command(
			"exec",
			code="(() => ({ ok: true, href: location.href, title: document.title }))()",
			workspace=workspace,
		)
		if exec_result.ok:
			tab_data = exec_result.data if isinstance(exec_result.data, dict) else {}
			checks.append(_bridge_check(
				"bridge_workspace",
				"ok",
				_workspace_detail(tab_data),
				None,
				{
					"workspace": workspace,
					"tab_url": _safe_url(tab_data.get("href")),
					"title_present": bool(tab_data.get("title")),
				},
			))
			checks.append(_bridge_check(
				"bridge_exec",
				"ok",
				"Bridge exec 基础能力可用",
				None,
			))
		else:
			recovery = _bridge_recovery_for_error(exec_result.error, workspace=workspace)
			checks.append(_bridge_check(
				"bridge_workspace",
				"warn",
				f"无法确认 workspace/tab 状态: {exec_result.error or 'unknown error'}",
				recovery,
			))
			checks.append(_bridge_check(
				"bridge_exec",
				"warn",
				f"Bridge exec 基础能力不可用: {exec_result.error or 'unknown error'}",
				recovery,
			))

		fetch_result = self.send_command(
			"exec",
			code=(
				"fetch('data:application/json,{\"ok\":true}')"
				".then(resp => resp.json())"
			),
			workspace=workspace,
		)
		checks.append(_bridge_check(
			"bridge_fetch",
			"ok" if fetch_result.ok else "warn",
			"Bridge fetch 基础能力可用"
			if fetch_result.ok else f"Bridge fetch 基础能力不可用: {fetch_result.error or 'unknown error'}",
			None if fetch_result.ok else _bridge_recovery_for_error(fetch_result.error, workspace=workspace),
		))

		if run_navigation_probe:
			navigate_result = self.send_command(
				"navigate",
				url=DIAGNOSTIC_NAVIGATE_URL,
				workspace=f"{workspace}-diagnostic",
			)
			checks.append(_bridge_check(
				"bridge_navigate",
				"ok" if navigate_result.ok else "warn",
				"Bridge navigate 基础能力可用"
				if navigate_result.ok else f"Bridge navigate 基础能力不可用: {navigate_result.error or 'unknown error'}",
				None if navigate_result.ok else _bridge_recovery_for_error(navigate_result.error, workspace=workspace),
			))
		else:
			checks.append(_bridge_check(
				"bridge_navigate",
				"skipped",
				"默认诊断不执行导航，避免改变现有 automation workspace/tab",
				None,
			))

		return checks

	def send_command(self, action: str, *, allow_replay: bool = True, **kwargs: Any) -> BridgeResult:
		"""发送命令到扩展。

		``allow_replay=False`` 用于导航、页面捕获和浏览器脚本：HTTP 超时只能说明
		客户端没收到结果，不能证明扩展没有执行。自动重发这类命令会重复刷新或
		重复触发翻页，因此必须把是否可重放作为调用方的显式语义。
		"""
		last_error = ""
		cmd_id = ""

		for attempt in range(1, self._max_retries + 1):
			cmd_id = make_command_id()
			cmd = BridgeCommand(id=cmd_id, action=action, **kwargs)

			try:
				resp = httpx.post(
					f"{self._base_url}{DAEMON_COMMAND_PATH}",
					json=cmd.to_dict(),
					timeout=self._timeout,
					trust_env=False,
				)
				payload = resp.json()
				result = BridgeResult.from_dict(payload)
				if (
					action in {"capture-visible-jobs", "capture-current-job-cards", "capture-visible-loaded-pages"} and not allow_replay
					and resp.status_code == 504 and payload.get("recoveryAvailable") is True
					and result.id == cmd_id
				):
					# The browser request already exists. Poll its local result only;
					# never resend /command or start a replacement page load.
					return self._await_capture_result(cmd_id)

				if result.ok:
					return result

				# 可重试的错误
				err = result.error or ""
				is_transient = any(k in err for k in (
					"Extension disconnected",
					"Extension not connected",
					"attach failed",
					"no longer exists",
					"Cannot debug tab",
				))
				if allow_replay and is_transient and attempt < self._max_retries:
					time.sleep(1.5)
					continue

				last_error = err
				break

			except (httpx.ConnectError, httpx.TimeoutException) as e:
				last_error = str(e)
				if allow_replay and attempt < self._max_retries:
					time.sleep(0.5)
					continue
				break
			except Exception as e:
				last_error = str(e)
				break

		return BridgeResult(id=cmd_id, ok=False, error=last_error)

	def _await_capture_result(self, cmd_id: str) -> BridgeResult:
		"""Recover a delayed reply from the local daemon, not from the website."""
		deadline = time.monotonic() + self._capture_recovery_timeout
		while time.monotonic() < deadline:
			try:
				response = httpx.post(
					f"{self._base_url}{DAEMON_RESULT_PATH}", json={"id": cmd_id},
					timeout=max(0.01, min(2.0, deadline - time.monotonic())), trust_env=False,
				)
				if response.status_code != 200:
					return BridgeResult(id=cmd_id, ok=False, error=f"Local capture recovery unavailable; command_id={cmd_id}; no browser request retried")
				payload = response.json()
				if not isinstance(payload, dict):
					raise ValueError("invalid recovery response")
				state = payload.get("state")
				if state == "completed":
					result = payload.get("result")
					if not isinstance(result, dict) or result.get("id") != cmd_id or type(result.get("ok")) is not bool:
						raise ValueError("recovery response identity or status mismatch")
					return BridgeResult.from_dict(result)
				if state != "pending":
					return BridgeResult(id=cmd_id, ok=False, error=f"Local capture result is unknown or expired; command_id={cmd_id}; no browser request retried")
			except httpx.TimeoutException:
				# A local observation timeout is not a reason to repeat the site action.
				pass
			except (httpx.HTTPError, ValueError, TypeError) as exc:
				return BridgeResult(id=cmd_id, ok=False, error=f"Local capture recovery failed: {exc}; command_id={cmd_id}; no browser request retried")
			remaining = deadline - time.monotonic()
			if remaining > 0:
				time.sleep(min(2.0, remaining))
		return BridgeResult(
			id=cmd_id, ok=False,
			error=f"Capture result remains pending after bounded local recovery; command_id={cmd_id}; execution result unknown; no browser request retried",
		)

	# ── 高级 API ─────────────────────────────────────────────────

	def evaluate(self, code: str, *, workspace: str = "boss") -> dict[str, Any]:
		"""在页面上下文中执行 JS，返回结果。"""
		result = self.send_command("exec", allow_replay=False, code=code, workspace=workspace)
		if not result.ok:
			raise RuntimeError(f"Bridge evaluate 失败: {result.error}")
		return result.data if isinstance(result.data, dict) else {"result": result.data}

	def navigate(self, url: str, *, workspace: str = "boss") -> dict[str, Any]:
		"""导航到指定 URL。"""
		if not _is_allowed_bridge_navigation(url):
			raise ValueError("Bridge 仅允许导航到 zhipin.com 或本地诊断地址")
		result = self.send_command("navigate", allow_replay=False, url=url, workspace=workspace)
		if not result.ok:
			raise RuntimeError(f"Bridge navigate 失败: {result.error}")
		return result.data if isinstance(result.data, dict) else {}

	def capture_job_list(
		self,
		url: str,
		*,
		page: int,
		workspace: str = "boss-radar",
		start_fresh: bool = False,
		allow_background_continuation: bool = False,
	) -> dict[str, Any]:
		"""Read visible BOSS state; later pages use one normal page-loader action.

		All pages use the fixed scripting operation. Never fall back to debugger
		reads, direct fetch, or the retired capture-job-list command.
		"""
		if not is_radar_search_url(url):
			raise ValueError("Bridge 页面采集仅允许包含单一城市和关键词的 BOSS HTTPS 搜索地址")
		if type(page) is not int or not 1 <= page <= 100:
			raise ValueError("page must be between 1 and 100")
		if type(start_fresh) is not bool:
			raise ValueError("start_fresh must be a boolean")
		if type(allow_background_continuation) is not bool:
			raise ValueError("allow_background_continuation must be a boolean")
		if start_fresh and page != 1:
			raise ValueError("start_fresh is only valid for page 1")
		if allow_background_continuation and page < 2:
			raise ValueError("allow_background_continuation is only valid after page 1")
		result = self.send_command(
			"capture-visible-jobs",
			allow_replay=False,
			url=url,
			page=page,
			workspace=workspace,
			start_fresh=start_fresh,
			allow_background_continuation=allow_background_continuation,
		)
		if not result.ok:
			raise RuntimeError(f"Bridge page capture 失败: {result.error}")
		return result.data if isinstance(result.data, dict) else {}

	def capture_current_job_cards(
		self,
		url: str,
		*,
		page: int = 1,
		workspace: str = "boss-radar",
		start_fresh: bool = True,
	) -> dict[str, Any]:
		"""Read a city result page, following only observed native next links.

		Later pages require the extension's exact sequential checkpoint. Commands
		are never replayed, and an unreadable window is not terminal evidence.
		"""
		if not is_radar_search_url(url):
			raise ValueError("Bridge 页面采集仅允许包含单一城市和关键词的 BOSS HTTPS 搜索地址")
		if type(start_fresh) is not bool:
			raise ValueError("start_fresh must be a boolean")
		if type(page) is not int or not 1 <= page <= 100:
			raise ValueError("page must be between 1 and 100")
		if start_fresh and page != 1:
			raise ValueError("start_fresh is only valid for page 1")
		result = self.send_command(
			"capture-current-job-cards",
			allow_replay=False,
			url=url,
			page=page,
			workspace=workspace,
			start_fresh=start_fresh,
		)
		if not result.ok:
			raise RuntimeError(f"Bridge current-layout capture 失败: {result.error}")
		return result.data if isinstance(result.data, dict) else {}

	def adopt_visible_pages(self, url: str, *, tab_id: int, page: int = 2) -> dict[str, Any]:
		"""Read existing cumulative batches once; never navigate, load, or replay."""
		if not is_radar_search_url(url) or type(tab_id) is not int or tab_id <= 0 or type(page) is not int or not 2 <= page <= 20:
			raise ValueError("Visible adoption requires an exact BOSS search URL, positive tab ID, and a settled page 2 through 20")
		result = self.send_command(
			"capture-visible-loaded-pages", allow_replay=False,
			workspace="boss-radar", url=url, tab_id=tab_id, page=page,
		)
		if not result.ok:
			raise RuntimeError(f"Bridge visible adoption failed: {result.error}")
		error = validate_visible_adoption(result.data, tab_id=tab_id, url=url, page=page)
		if error:
			raise RuntimeError(error)
		return result.data

	def radar_tab_status(self) -> dict[str, Any]:
		"""Return the explicitly user-claimed BOSS radar tab state."""
		result = self.send_command("radar-tab-status", allow_replay=True, workspace="boss-radar")
		if not result.ok:
			raise RuntimeError(f"Bridge radar tab status 失败: {result.error}")
		return result.data if isinstance(result.data, dict) else {}

	def present_radar_tab(self, tab_id: int) -> dict[str, Any]:
		"""Present the already-claimed tab once; never open/reload or replay.

		Visibility changes may allow a previously pending page action to settle.
		Callers must inspect the resulting page, not automatically send load-more.
		"""
		if type(tab_id) is not int or tab_id <= 0:
			raise ValueError("tab_id must be a positive integer")
		result = self.send_command(
			"present-radar-tab", allow_replay=False, workspace="boss-radar", tab_id=tab_id,
		)
		if not result.ok:
			raise RuntimeError(f"Bridge present radar tab failed: {result.error}")
		if not isinstance(result.data, dict):
			raise RuntimeError("Bridge present radar tab returned invalid state")
		return result.data

	def close_window(self, *, workspace: str = "boss") -> None:
		"""关闭 automation window。"""
		self.send_command("close-window", allow_replay=False, workspace=workspace)

	def fetch_json(self, url: str, *, method: str = "GET", data: dict[str, Any] | None = None, referer: str = "", workspace: str = "boss") -> dict[str, Any]:
		"""通过浏览器 fetch() 发起 API 请求，返回 JSON。"""
		if not _is_zhipin_https_url(url):
			raise ValueError("Bridge fetch 仅允许访问 zhipin.com HTTPS 地址")
		if method == "GET":
			js = f"""
				(async () => {{
					const resp = await fetch({json.dumps(url)}, {{
						method: 'GET',
						credentials: 'include',
						headers: {{
							'Accept': 'application/json',
							'X-Requested-With': 'XMLHttpRequest',
							{f"'Referer': {json.dumps(referer)}," if referer else ""}
						}},
					}});
					return await resp.json();
				}})()
			"""
		else:
			form_entries = "\n".join(
				f"formData.append({json.dumps(k)}, {json.dumps(str(v))});"
				for k, v in (data or {}).items()
			)
			js = f"""
				(async () => {{
					const formData = new URLSearchParams();
					{form_entries}
					const resp = await fetch({json.dumps(url)}, {{
						method: 'POST',
						credentials: 'include',
						headers: {{
							'Accept': 'application/json',
							'Content-Type': 'application/x-www-form-urlencoded',
							'X-Requested-With': 'XMLHttpRequest',
							{f"'Referer': {json.dumps(referer)}," if referer else ""}
						}},
						body: formData.toString(),
					}});
					return await resp.json();
				}})()
			"""
		return self.evaluate(js, workspace=workspace)


def _is_zhipin_https_url(value: str) -> bool:
	try:
		parsed = urlparse(value)
	except ValueError:
		return False
	host = (parsed.hostname or "").rstrip(".").lower()
	return parsed.scheme == "https" and (host == "zhipin.com" or host.endswith(".zhipin.com"))


def _is_allowed_bridge_navigation(value: str) -> bool:
	return _is_zhipin_https_url(value) or value == DIAGNOSTIC_NAVIGATE_URL


def _bridge_check(
	name: str,
	status: str,
	detail: str,
	recovery_action: str | None = None,
	extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
	check: dict[str, Any] = {
		"name": name,
		"status": status,
		"detail": detail,
	}
	if recovery_action:
		check["recovery_action"] = recovery_action
		check["hint"] = recovery_action
	if extra:
		check.update(extra)
	return check


def _is_supported_extension_version(version: str) -> bool:
	if not version:
		return False
	return version.split(".", 1)[0] == SUPPORTED_EXTENSION_MAJOR_VERSION


def _safe_url(value: Any) -> str | None:
	if not isinstance(value, str) or not value:
		return None
	if value.startswith(("http://", "https://")):
		return value.split("#", 1)[0]
	if value.startswith("data:"):
		return "data:<local>"
	return "<non-http>"


def _workspace_detail(data: dict[str, Any]) -> str:
	url = _safe_url(data.get("href"))
	if url:
		return f"Bridge workspace/tab 可用: {url}"
	return "Bridge workspace/tab 可用"


def _bridge_recovery_for_error(error: str, *, workspace: str) -> str:
	err = (error or "").lower()
	if "extension not connected" in err or "extension disconnected" in err:
		return "确认 Chrome 扩展已启用并连接本地 daemon，然后重新运行 boss doctor"
	if "cannot debug tab" in err or "url is" in err:
		return f"打开目标页面或允许 Bridge 创建 workspace={workspace} 的 http(s) 标签页"
	if "attach failed" in err or "another debugger" in err:
		return "关闭其他调试器或 DevTools 后重试；不要用 Bridge 重试平台风控拦截"
	if "timed out" in err:
		return "检查页面是否卡住，重新加载扩展或重启 Bridge daemon"
	return "检查 Bridge daemon、Chrome 扩展和目标页面状态后重试"
