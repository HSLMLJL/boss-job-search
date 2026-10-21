"""Browser Bridge daemon — HTTP + WebSocket 服务。

接收 CLI 的 HTTP 命令，转发给 Chrome 扩展（WebSocket），返回结果。
首次浏览器命令时自动启动，空闲 4h 后自动退出。

依赖基础安装自带的 aiohttp；浏览器客户端在需要时自动启动本地 daemon。
"""

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

_PID_FILE = Path.home() / ".boss-agent" / "bridge" / "daemon.pid"
_LOG_FILE = Path.home() / ".boss-agent" / "bridge" / "daemon.log"
_COMMAND_AUDIT_FILE = Path.home() / ".boss-agent" / "bridge-command-audit.jsonl"
_COMMAND_AUDIT_MAX_BYTES = 4 * 1024 * 1024
_ALLOWED_COMMAND_ACTIONS = frozenset(
    {"exec", "navigate", "capture-job-list", "capture-visible-jobs", "capture-current-job-cards", "capture-visible-loaded-pages", "inspect-radar-page", "present-radar-tab", "radar-tab-status", "close-window", "verify-job-link", "read-search-job-panel"}
)
_MAX_COMMAND_TEXT = 64 * 1024
_COMMAND_TIMEOUT_SECONDS = 60.0
_INSPECT_DIAGNOSTIC_STAGES = frozenset({
	"received", "before-tab-get", "execute-script-dispatched", "execute-script-settled",
	"after-tab-get", "result-ready",
})
_MAX_RECENT_INSPECT_DIAGNOSTICS = 32
_DIAGNOSED_PAGE_ACTIONS = frozenset({"inspect-radar-page", "capture-visible-jobs", "capture-current-job-cards", "capture-visible-loaded-pages", "present-radar-tab"})
_VISIBLE_CAPTURE_ACTIONS = frozenset({"capture-visible-jobs", "capture-current-job-cards", "capture-visible-loaded-pages"})
_CAPTURE_RESULT_JOURNAL_LIMIT = 16
_CAPTURE_RESULT_TTL_SECONDS = 10 * 60.0
_CAPTURE_RESULT_MAX_BYTES = 2 * 1024 * 1024
_SENSITIVE_AUDIT_VALUE = re.compile(
        r"(?i)(?:token|cookie|session|auth(?:orization)?|password|secret|api[_-]?key)"
)


class _ExtensionHandshake:
	"""Identity asserted by the currently connected extension only."""

	def __init__(self) -> None:
		self.reset()

	def reset(self) -> None:
		self.version: str | None = None
		self.extension_id: str | None = None
		self.extension_build_hash: str | None = None
		self.origin_extension_id: str | None = None
		self.hello_received = False

	def begin_connection(self, origin: str | None) -> None:
		self.reset()
		self.origin_extension_id = _extension_id_from_origin(origin)

	def update_from_hello(self, data: dict[str, Any]) -> None:
		# `version` predates the integrity handshake and remains compatible with
		# older extensions. New fields intentionally stay null when absent or bad.
		version = data.get("version", "unknown")
		self.version = version if isinstance(version, str) else "unknown"
		extension_id = data.get("extensionId")
		build_hash = data.get("extensionBuildHash")
		self.extension_id = extension_id if isinstance(extension_id, str) and extension_id else None
		self.extension_build_hash = build_hash if isinstance(build_hash, str) and build_hash else None
		self.hello_received = True

	def matches_radar_package(self, *, version: str, build_hash: str) -> bool:
		"""True only for a complete, current-socket, valid integrity hello."""
		return bool(
			self.hello_received
			and self.origin_extension_id is not None
			and self.version == version
			and self.extension_id == self.origin_extension_id
			and isinstance(self.extension_id, str)
			and re.fullmatch(r"[a-p]{32}", self.extension_id)
			and self.extension_build_hash == build_hash
			and isinstance(self.extension_build_hash, str)
			and re.fullmatch(r"[0-9a-f]{64}", self.extension_build_hash)
		)


class _PendingCommands:
	"""Keep each command result bound to the extension socket that received it."""

	def __init__(self) -> None:
		self._commands: dict[str, tuple["asyncio.Future[Any]", Any]] = {}

	def contains(self, command_id: str) -> bool:
		return command_id in self._commands

	def add(self, command_id: str, future: "asyncio.Future[Any]", owner: Any) -> None:
		self._commands[command_id] = (future, owner)

	def remove(self, command_id: str) -> None:
		self._commands.pop(command_id, None)

	def resolve_from(self, command_id: str, result: dict[str, Any], owner: Any) -> bool:
		entry = self._commands.get(command_id)
		if entry is None or entry[1] is not owner:
			return False
		future = entry[0]
		if not future.done():
			future.set_result(result)
		return True

	def disconnect(self, owner: Any) -> None:
		for future, command_owner in list(self._commands.values()):
			if command_owner is owner and not future.done():
				future.set_result({"id": "", "ok": False, "error": "Extension disconnected"})


class _RecentInspectDiagnostics:
	"""Bounded, socket-owned metadata for the read-only page probe.

	Never retain extension result data, page contents, or socket objects in the
	public snapshot.  A late browser result is useful evidence after a local HTTP
	timeout, so entries intentionally outlive their matching pending future.
	"""

	def __init__(self, limit: int = _MAX_RECENT_INSPECT_DIAGNOSTICS) -> None:
		self._limit = limit
		self._entries: dict[str, dict[str, Any]] = {}
		self._owners: dict[str, Any] = {}
		self._order: list[str] = []

	def start(self, command_id: str, owner: Any, *, action: str = "inspect-radar-page") -> None:
		if action not in _DIAGNOSED_PAGE_ACTIONS:
			raise ValueError("Unsupported page diagnostic action")
		if command_id in self._entries:
			self._order.remove(command_id)
		self._entries[command_id] = {
			"id": command_id,
			"action": action,
			"state": "pending",
			"lastStage": "daemon-sent",
			"timedOut": False,
			"resultReceived": False,
			"lateResult": False,
		}
		self._owners[command_id] = owner
		self._order.append(command_id)
		while len(self._order) > self._limit:
			oldest = self._order.pop(0)
			self._entries.pop(oldest, None)
			self._owners.pop(oldest, None)

	def record_stage(self, command_id: str, stage: str, owner: Any) -> bool:
		if stage not in _INSPECT_DIAGNOSTIC_STAGES or self._owners.get(command_id) is not owner:
			return False
		entry = self._entries.get(command_id)
		if entry is None:
			return False
		entry["lastStage"] = stage
		return True

	def record_timeout(self, command_id: str, owner: Any) -> str:
		if self._owners.get(command_id) is not owner or command_id not in self._entries:
			return "unknown"
		entry = self._entries[command_id]
		entry["timedOut"] = True
		entry["state"] = "result-unknown"
		return str(entry["lastStage"])

	def record_result(self, command_id: str, result: dict[str, Any], owner: Any) -> bool:
		if self._owners.get(command_id) is not owner:
			return False
		entry = self._entries.get(command_id)
		if entry is None:
			return False
		entry["resultReceived"] = True
		entry["lateResult"] = bool(entry["timedOut"])
		entry["state"] = "late-result" if entry["timedOut"] else "completed"
		entry["resultOk"] = result.get("ok") if isinstance(result.get("ok"), bool) else False
		return True

	def record_disconnect(self, owner: Any) -> None:
		for command_id, command_owner in self._owners.items():
			if command_owner is owner and self._entries[command_id]["state"] == "pending":
				self._entries[command_id]["state"] = "disconnected"

	def snapshot(self) -> list[dict[str, Any]]:
		return [dict(self._entries[command_id]) for command_id in self._order]


class _CaptureResultJournal:
	"""Short-lived, socket-owned recovery cache for visible capture results."""

	def __init__(
		self, limit: int = _CAPTURE_RESULT_JOURNAL_LIMIT, ttl: float = _CAPTURE_RESULT_TTL_SECONDS,
		max_bytes: int = _CAPTURE_RESULT_MAX_BYTES,
	) -> None:
		self._limit = limit
		self._ttl = ttl
		self._max_bytes = max_bytes
		self._entries: dict[str, dict[str, Any]] = {}
		self._order: list[str] = []

	def _prune(self, now: float | None = None) -> None:
		now = time.monotonic() if now is None else now
		for command_id in list(self._order):
			entry = self._entries.get(command_id)
			if entry is None or entry["expiresAt"] <= now:
				self._entries.pop(command_id, None)
				self._order.remove(command_id)

	def contains(self, command_id: str) -> bool:
		self._prune()
		return command_id in self._entries

	def start(self, command_id: str, owner: Any) -> bool:
		self._prune()
		if command_id in self._entries:
			return False
		while len(self._order) >= self._limit:
			oldest = self._order.pop(0)
			self._entries.pop(oldest, None)
		self._entries[command_id] = {
			"owner": owner,
			"state": "pending",
			"expiresAt": time.monotonic() + self._ttl,
		}
		self._order.append(command_id)
		return True

	def record_result(self, command_id: str, result: dict[str, Any], owner: Any) -> bool:
		self._prune()
		entry = self._entries.get(command_id)
		if (
			entry is None or entry["owner"] is not owner or entry["state"] != "pending"
			or result.get("id") != command_id or type(result.get("ok")) is not bool
		):
			return False
		try:
			size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
		except (TypeError, ValueError):
			size = self._max_bytes + 1
		if size > self._max_bytes:
			entry["state"] = "failed"
			entry.pop("result", None)
			return False
		entry["state"] = "completed"
		entry["result"] = result
		return True

	def record_failed(self, command_id: str, owner: Any) -> None:
		entry = self._entries.get(command_id)
		if entry is not None and entry["owner"] is owner and entry["state"] == "pending":
			entry["state"] = "failed"

	def disconnect(self, owner: Any) -> None:
		for entry in self._entries.values():
			if entry["owner"] is owner and entry["state"] == "pending":
				entry["state"] = "failed"

	def lookup(self, command_id: str) -> dict[str, Any]:
		self._prune()
		entry = self._entries.get(command_id)
		if entry is None or entry["state"] == "failed":
			return {"state": "unknown"}
		if entry["state"] == "pending":
			return {"state": "pending"}
		return {"state": "completed", "result": entry["result"]}


class _VisibleCaptureSequence:
	"""Per-extension-socket proof that visible capture pages are sequential."""

	def __init__(self) -> None:
		self._checkpoints: list[dict[str, Any]] = []
		self._pending: dict[str, dict[str, Any]] = {}

	@staticmethod
	def _matches(entry: dict[str, Any], owner: Any, workspace: str, url: str) -> bool:
		return entry["owner"] is owner and entry["workspace"] == workspace and entry["url"] == url

	def _checkpoint(self, owner: Any, workspace: str, url: str) -> dict[str, Any] | None:
		return next((entry for entry in self._checkpoints if self._matches(entry, owner, workspace, url)), None)

	def admit(self, command: dict[str, Any], owner: Any) -> str | None:
		"""Reserve a command without advancing any page checkpoint."""
		command_id = command["id"]
		workspace = command["workspace"]
		url = command["url"]
		page = command["page"]
		if any(entry["owner"] is owner and entry["workspace"] == workspace for entry in self._pending.values()):
			return "Visible capture already has an unresolved command on this extension connection"
		if command.get("action") == "capture-visible-loaded-pages":
			# Atomic read-only re-anchoring is permitted after a connection restart.
			# Do not establish the new checkpoint until its entire batch proof arrives.
			self._pending[command_id] = {"owner": owner, "workspace": workspace, "url": url,
				"page": page, "tabId": command["tabId"], "action": command["action"]}
			return None
		if page == 1:
			# A fresh first page deliberately discards this route's old progress, but
			# does not create a new checkpoint until its success result arrives.
			self._checkpoints = [entry for entry in self._checkpoints if not self._matches(entry, owner, workspace, url)]
		else:
			checkpoint = self._checkpoint(owner, workspace, url)
			if checkpoint is None:
				return "Visible capture sequence is not established on this extension connection; start at page 1"
			if page != checkpoint["page"] + 1:
				return f"Visible capture requires page {checkpoint['page'] + 1} after the last confirmed page"
		self._pending[command_id] = {"owner": owner, "workspace": workspace, "url": url, "page": page}
		return None

	def record_result(self, command_id: str, result: dict[str, Any], owner: Any) -> bool:
		entry = self._pending.get(command_id)
		if entry is None or entry["owner"] is not owner:
			return False
		self._pending.pop(command_id, None)
		data = result.get("data")
		if entry.get("action") == "capture-visible-loaded-pages":
			from boss_agent_cli.bridge.protocol import validate_visible_adoption
			if (result.get("id") != command_id or result.get("ok") is not True
				or validate_visible_adoption(data, tab_id=entry["tabId"], url=entry["url"], page=entry["page"])):
				return False
			self._checkpoints = [state for state in self._checkpoints if not self._matches(
				state, owner, entry["workspace"], entry["url"])]
			self._checkpoints.append(entry)
			return True
		zp_data = data.get("zpData") if isinstance(data, dict) else None
		page_state = zp_data.get("pageState") if isinstance(zp_data, dict) else None
		if not (
			result.get("id") == command_id and result.get("ok") is True
			and isinstance(data, dict) and type(data.get("code")) is int and data.get("code") == 0
			and isinstance(zp_data, dict) and isinstance(zp_data.get("jobList"), list)
			and type(zp_data.get("hasMore")) is bool
			and isinstance(page_state, dict)
			and type(page_state.get("currentPage")) is int and page_state.get("currentPage") == entry["page"]
			and page_state.get("identityMatched") is True
			and type(page_state.get("hasMore")) is bool and page_state.get("hasMore") is zp_data.get("hasMore")
			and (
				page_state.get("pageSize") is None
				or (type(page_state.get("pageSize")) is int and 1 <= page_state.get("pageSize") <= 100)
			)
			and (zp_data.get("hasMore") is True or page_state.get("terminal") is True)
		):
			return False
		self._checkpoints = [state for state in self._checkpoints if not self._matches(
			state, entry["owner"], entry["workspace"], entry["url"]
		)]
		self._checkpoints.append(entry)
		return True

	def record_failed(self, command_id: str, owner: Any) -> None:
		entry = self._pending.get(command_id)
		if entry is not None and entry["owner"] is owner:
			self._pending.pop(command_id, None)

	def disconnect(self, owner: Any) -> None:
		self._checkpoints = [entry for entry in self._checkpoints if entry["owner"] is not owner]
		self._pending = {command_id: entry for command_id, entry in self._pending.items() if entry["owner"] is not owner}


def _extension_id_from_origin(value: str | None) -> str | None:
	if not value or not value.startswith("chrome-extension://"):
		return None
	extension_id = value.removeprefix("chrome-extension://").rstrip("/")
	return extension_id if re.fullmatch(r"[a-p]{32}", extension_id) else None


def _packaged_extension_identity() -> tuple[str, str]:
	"""Read the extension shipped with this CLI, falling back to a checkout."""
	package_path = Path(__file__).resolve().parents[1] / "bridge_extension"
	source_path = Path(__file__).resolve().parents[3] / "extension"
	path = package_path if (package_path / "manifest.json").is_file() else source_path
	manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
	version = manifest.get("version")
	if not isinstance(version, str) or not version:
		raise ValueError("Packaged extension manifest has no valid version")
	return version, hashlib.sha256((path / "background.js").read_bytes()).hexdigest()


def _requires_radar_integrity(command: dict[str, Any]) -> bool:
	return (
		command.get("workspace") == "boss-radar"
                and command.get("action") in {"exec", "navigate", "capture-job-list", "capture-visible-jobs", "capture-current-job-cards", "capture-visible-loaded-pages", "inspect-radar-page", "present-radar-tab", "read-search-job-panel"}
	)


def _radar_integrity_error(
	command: dict[str, Any], handshake: _ExtensionHandshake, *, version: str, build_hash: str,
) -> str | None:
	"""Return an execution barrier error, or None when this command is authorised."""
	if not _requires_radar_integrity(command):
		return None
	if not handshake.matches_radar_package(version=version, build_hash=build_hash):
		return "Radar extension integrity handshake is missing or mismatched"
	return None


def _is_extension_origin(value: str | None) -> bool:
        """Only a Chrome extension page may claim the daemon extension socket."""
        return _extension_id_from_origin(value) is not None


def _is_loopback_url(value: str) -> bool:
        """Whether a URL addresses localhost without resolving arbitrary hosts."""
        try:
                host = urlsplit(value).hostname
                if not host:
                        return False
                normalized = host.rstrip(".").lower()
                if normalized == "localhost" or normalized.endswith(".localhost"):
                        return True
                return ip_address(normalized).is_loopback
        except ValueError:
                return False


def _audit_text(value: Any, *, limit: int = 160) -> str | None:
        """Keep useful search metadata without retaining credentials or tokens."""
        if not isinstance(value, str) or not value:
                return None
        if _SENSITIVE_AUDIT_VALUE.search(value):
                return "[redacted]"
        return value[:limit]


def _audit_url(value: Any) -> tuple[str | None, str | None, str | None]:
        """Return only URL origin/path and the fixed radar city/query fields."""
        if not isinstance(value, str) or not value:
                return None, None, None
        try:
                parsed = urlsplit(value)
                if not parsed.scheme or not parsed.hostname:
                        return None, None, None
                host = parsed.hostname.lower()
                if parsed.port is not None:
                        host = f"{host}:{parsed.port}"
                safe_url = f"{parsed.scheme.lower()}://{host}{parsed.path or '/'}"
                query = parse_qs(parsed.query, keep_blank_values=True)
                city_values = query.get("city", [])
                query_values = query.get("query", [])
                city = _audit_text(city_values[0]) if len(city_values) == 1 else None
                search_query = _audit_text(query_values[0]) if len(query_values) == 1 else None
                return safe_url, city, search_query
        except (TypeError, ValueError):
                return None, None, None


def _audit_error_summary(value: Any) -> str | None:
        """A bounded, credential-safe error summary; never retain command body/code."""
        if not isinstance(value, str) or not value:
                return None
        value = re.sub(
                r"(?i)((?:token|cookie|session|auth(?:orization)?|password|secret|api[_-]?key)\s*[=:]\s*)[^\s&,'\"]+",
                r"\1[redacted]",
                value,
        )
        return value[:240]


def _write_command_audit(
        command: Any,
        *,
        result: str,
        error: Any = None,
) -> None:
        """Best-effort append-only local command audit, deliberately data-minimal.

        The audit must never alter command execution or trigger a retry.  It stops
        appending once bounded instead of rotating/deleting a user's old evidence.
        """
        try:
                if _COMMAND_AUDIT_FILE.exists() and _COMMAND_AUDIT_FILE.stat().st_size >= _COMMAND_AUDIT_MAX_BYTES:
                        print("[bridge] command audit is full; preserving existing audit without rotation", flush=True)
                        return
                payload = command if isinstance(command, dict) else {}
                safe_url, city, search_query = _audit_url(payload.get("url"))
                entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "commandId": payload.get("id") if isinstance(payload.get("id"), str) else None,
                        "action": payload.get("action") if isinstance(payload.get("action"), str) else None,
                        "workspace": payload.get("workspace") if isinstance(payload.get("workspace"), str) else None,
			"tabId": payload.get("tabId") if type(payload.get("tabId")) is int else None,
			"page": payload.get("page") if type(payload.get("page")) is int else None,
			"startFresh": payload.get("startFresh") if type(payload.get("startFresh")) is bool else None,
			"allowBackgroundContinuation": payload.get("allowBackgroundContinuation")
			if type(payload.get("allowBackgroundContinuation")) is bool else None,
			"daemonPid": os.getpid(),
                        "url": safe_url,
                        "city": city,
                        "query": search_query,
                        # HTTP does not reliably identify the local client process.
                        "clientPid": None,
                        "result": result,
                        "error": _audit_error_summary(error),
                }
                _COMMAND_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
                with _COMMAND_AUDIT_FILE.open("a", encoding="utf-8") as audit_file:
                        audit_file.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
                print(f"[bridge] command audit write failed: {exc}", flush=True)


def _validate_command_payload(value: Any) -> str | None:
	"""Return an error message for malformed/oversized local commands."""
	if not isinstance(value, dict):
		return "Command must be a JSON object"
	command_id = value.get("id")
	action = value.get("action")
	if not isinstance(command_id, str) or not command_id or len(command_id) > 64:
		return "Invalid command id"
	if action not in _ALLOWED_COMMAND_ACTIONS:
		return "Invalid command action"
	if action == "read-search-job-panel":
		from boss_agent_cli.bridge.protocol import is_radar_search_url
		if set(value) - {"id", "action", "tabId", "workspace", "url", "panelMode", "expectedJobId"}:
			return "Search panel accepts fixed DOM operations only"
		if value.get("workspace") != "boss-radar" or type(value.get("tabId")) is not int or value["tabId"] <= 0:
			return "Search panel requires the claimed boss-radar tab"
		if not is_radar_search_url(value.get("url", "")):
			return "Search panel requires an exact HTTPS search URL"
		if value.get("panelMode", "read") not in {"read", "inspect", "select"}:
			return "Invalid search panel operation"
		job_id = value.get("expectedJobId", "")
		if not isinstance(job_id, str) or len(job_id) > 160 or (job_id and not re.fullmatch(r"[A-Za-z0-9_-]+", job_id)):
			return "Invalid search panel job identity"
		if value.get("panelMode") == "select" and not job_id:
			return "Card selection requires an exact stable job ID"
	if action == "verify-job-link":
		from boss_agent_cli.radar.link_verification import boss_stable_job_id
		try:
			if set(value) - {"id", "action", "tabId", "workspace", "url", "code"}:
				return "Link verification only accepts fixed target and anchor metadata"
			if type(value.get("tabId")) is not int or value["tabId"] <= 0:
				return "Link verification requires an explicit positive anchor tabId"
			workspace = value.get("workspace", "")
			if not isinstance(workspace, str) or not workspace.startswith("boss-verify-links-") or len(workspace) > 100:
				return "Link verification requires its isolated workspace"
			code = value.get("code", "")
			if not isinstance(code, str) or len(code) > 2048:
				return "Invalid link verification anchor metadata"
			anchor = json.loads(code)
			if not isinstance(anchor, dict) or set(anchor) != {"sourceUrl"}:
				return "Link verification does not accept executable scripts"
			boss_stable_job_id(value.get("url"))
			boss_stable_job_id(anchor.get("sourceUrl"))
		except (ValueError, TypeError, KeyError):
			return "Link verification requires canonical HTTPS BOSS detail URLs"
	for field in ("code", "url", "workspace"):
		field_value = value.get(field, "")
		if not isinstance(field_value, str) or len(field_value) > _MAX_COMMAND_TEXT:
			return f"Invalid command field: {field}"
	if "tabId" in value and (not isinstance(value["tabId"], int) or value["tabId"] <= 0):
		return "Invalid command field: tabId"
	if (
		action == "navigate"
		and value.get("workspace") == "boss-radar"
		and _is_loopback_url(value.get("url", ""))
	):
		return "boss-radar navigate cannot target a loopback diagnostic URL"
	if action in {"inspect-radar-page", "present-radar-tab"}:
		# Fixed operations, never an arbitrary script/navigation escape hatch.
		# Present may only focus the existing claimed radar tab; the extension
		# validates ownership and the live BOSS URL before any window action.
		if type(value.get("tabId")) is not int or value["tabId"] <= 0:
			return f"{action} requires an explicit positive tabId"
		if value.get("workspace") != "boss-radar":
			return f"{action} requires boss-radar workspace"
		if set(value) - {"id", "action", "tabId", "workspace"}:
			return f"{action} does not accept scripts, navigation, or paging fields"
	if action == "capture-visible-loaded-pages":
		from boss_agent_cli.bridge.protocol import is_radar_search_url
		if value.get("workspace") != "boss-radar" or type(value.get("tabId")) is not int or value["tabId"] <= 0:
			return "Visible adoption requires the boss-radar workspace and explicit positive tabId"
		if set(value) - {"id", "action", "workspace", "tabId", "url", "page"}:
			return "Visible adoption does not accept scripts, navigation, or fresh fields"
		if not is_radar_search_url(value.get("url", "")) or type(value.get("page")) is not int or not 2 <= value["page"] <= 20:
			return "Visible adoption requires an exact BOSS search URL and a settled page 2 through 20"
	if action in {"capture-visible-jobs", "capture-current-job-cards"}:
		from boss_agent_cli.bridge.protocol import is_radar_search_url

		if value.get("workspace") != "boss-radar":
			return "capture-visible-jobs requires boss-radar workspace"
		allowed = {"id", "action", "tabId", "workspace", "url", "page", "startFresh"}
		if action == "capture-visible-jobs":
			allowed.add("allowBackgroundContinuation")
		if set(value) - allowed:
			return f"{action} accepts only fixed search and paging fields"
		if not is_radar_search_url(value.get("url", "")):
			return f"{action} requires an HTTPS BOSS jobs URL with one city and query"
		maximum_page = 100
		if type(value.get("page")) is not int or not 1 <= value["page"] <= maximum_page:
			return "Invalid command field: page"
		if "tabId" in value and type(value["tabId"]) is not int:
			return "Invalid command field: tabId"
	if "page" in value and (not isinstance(value["page"], int) or not 1 <= value["page"] <= 100):
		return "Invalid command field: page"
	if "startFresh" in value:
		if not isinstance(value["startFresh"], bool):
			return "Invalid command field: startFresh"
		if value["startFresh"] and (
			action not in {"capture-job-list", "capture-visible-jobs", "capture-current-job-cards"}
			or value.get("page") != 1
			or value.get("workspace") != "boss-radar"
		):
			return "startFresh is only allowed for boss-radar capture page 1"
	if "allowBackgroundContinuation" in value:
		if type(value["allowBackgroundContinuation"]) is not bool:
			return "Invalid command field: allowBackgroundContinuation"
		if value["allowBackgroundContinuation"] and (
			action != "capture-visible-jobs"
			or value.get("page") is None
			or value.get("page") < 2
			or value.get("workspace") != "boss-radar"
		):
			return "allowBackgroundContinuation is only allowed for boss-radar capture page 2 or later"
	return None


def _pid_exists(pid: int) -> bool:
	"""Cross-platform liveness probe that does not signal the target process."""
	if pid <= 0:
		return False
	if sys.platform == "win32":
		import ctypes
		from ctypes import wintypes

		process_query_limited_information = 0x1000
		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
		kernel32.OpenProcess.restype = wintypes.HANDLE
		kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
		kernel32.CloseHandle.restype = wintypes.BOOL
		handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
		if handle:
			kernel32.CloseHandle(handle)
			return True
		# Access denied still means the process exists under another integrity level.
		return ctypes.get_last_error() == 5
	try:
		os.kill(pid, 0)
		return True
	except OSError:
		return False


def _ensure_dirs() -> None:
	_PID_FILE.parent.mkdir(parents=True, exist_ok=True)


def is_daemon_running() -> bool:
	"""检查 daemon 是否在运行。"""
	if not _PID_FILE.exists():
		return False
	try:
		pid = int(_PID_FILE.read_text().strip())
		if not _pid_exists(pid):
			raise ProcessLookupError(pid)
		return True
	except (OSError, ValueError):
		_PID_FILE.unlink(missing_ok=True)
		return False


def get_daemon_pid() -> int | None:
	"""获取 daemon PID，不运行则返回 None。"""
	if not _PID_FILE.exists():
		return None
	try:
		pid = int(_PID_FILE.read_text().strip())
		if not _pid_exists(pid):
			raise ProcessLookupError(pid)
		return pid
	except (OSError, ValueError):
		_PID_FILE.unlink(missing_ok=True)
		return None


def stop_daemon() -> bool:
	"""停止 daemon 进程。"""
	pid = get_daemon_pid()
	if pid is None:
		return False
	try:
		os.kill(pid, signal.SIGTERM)
		for _ in range(20):
			if not _pid_exists(pid):
				break
			time.sleep(0.1)
		_PID_FILE.unlink(missing_ok=True)
		return True
	except OSError:
		_PID_FILE.unlink(missing_ok=True)
		return False


def start_daemon_background() -> int | None:
	"""在后台启动 daemon 进程，返回实际 daemon PID。跨平台兼容。"""
	if is_daemon_running():
		return get_daemon_pid()

	_ensure_dirs()

	# 跨平台后台启动：用 subprocess.Popen 替代 os.fork
	kwargs: dict[str, Any] = {}
	if sys.platform == "win32":
		# Windows: DETACHED_PROCESS 标志
		kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
	else:
		# Unix: 新会话脱离终端
		kwargs["start_new_session"] = True

	with open(_LOG_FILE, "a") as log_fd:
		proc = subprocess.Popen(
			[sys.executable, "-m", "boss_agent_cli.bridge.daemon", "--serve"],
			stdout=log_fd,
			stderr=log_fd,
			stdin=subprocess.DEVNULL,
			**kwargs,
		)

	# 等待 PID 文件出现（daemon 启动后写入）
	for _ in range(20):
		time.sleep(0.25)
		pid = get_daemon_pid()
		if pid is not None:
			return pid

	# fallback: 返回 Popen 的 PID
	return proc.pid


async def _run_daemon() -> None:
	"""daemon 主循环：统一 aiohttp HTTP + WebSocket 服务。"""
	from aiohttp import web

	from boss_agent_cli.bridge.protocol import (
		BRIDGE_HOST, BRIDGE_PORT, DAEMON_IDLE_TIMEOUT,
		DAEMON_WS_PATH, DAEMON_PING_PATH, DAEMON_STATUS_PATH, DAEMON_COMMAND_PATH, DAEMON_RESULT_PATH,
	)

	start_time = time.time()
	ext_ws: Any = None
	handshake = _ExtensionHandshake()
	last_activity = time.time()
	pending_commands = _PendingCommands()
	inspect_diagnostics = _RecentInspectDiagnostics()
	capture_results = _CaptureResultJournal()
	visible_capture_sequence = _VisibleCaptureSequence()

	# ── HTTP handlers ─────────────────────────────────────────────

	async def handle_ping(request: Any) -> Any:
		nonlocal last_activity
		last_activity = time.time()
		return web.json_response({"ok": True})

	async def handle_status(request: Any) -> Any:
		nonlocal last_activity
		last_activity = time.time()
		return web.json_response({
			"ok": True,
			"extensionConnected": ext_ws is not None,
			"extensionVersion": handshake.version,
			"extensionId": handshake.extension_id,
			"extensionBuildHash": handshake.extension_build_hash,
			# Bounded command metadata only; page data and extension messages are
			# never exposed through this local diagnostic endpoint.
			"recentInspectDiagnostics": inspect_diagnostics.snapshot(),
			"pid": os.getpid(),
			"uptime": int(time.time() - start_time),
		})

	async def handle_command_result(request: Any) -> Any:
		# This endpoint is intentionally for the local CLI only. Browser fetch
		# metadata is rejected before parsing, like /command.
		if "Origin" in request.headers or "Sec-Fetch-Site" in request.headers:
			return web.json_response({"state": "unknown", "error": "Browser-origin recovery is forbidden"}, status=403)
		try:
			payload = await request.json()
		except (ValueError, KeyError, TypeError):
			return web.json_response({"state": "unknown", "error": "Invalid JSON"}, status=400)
		command_id = payload.get("id") if isinstance(payload, dict) and set(payload) == {"id"} else None
		if not isinstance(command_id, str) or not command_id or len(command_id) > 64:
			return web.json_response({"state": "unknown", "error": "Invalid command id"}, status=400)
		return web.json_response(capture_results.lookup(command_id))

	async def handle_command(request: Any) -> Any:
		nonlocal last_activity
		# Browser pages always attach Origin to cross-origin POSTs. Reject them
		# before parsing so a malicious website cannot drive the local Bridge.
		# The Python CLI uses loopback HTTP without an Origin header.
		if request.headers.get("Origin"):
			_write_command_audit(None, result="rejected", error="Browser-origin commands are forbidden")
			return web.json_response(
				{"id": "", "ok": False, "error": "Browser-origin commands are forbidden"},
				status=403,
			)

		try:
			cmd = await request.json()
		except (ValueError, KeyError, TypeError):
			_write_command_audit(None, result="rejected", error="Invalid JSON")
			return web.json_response(
				{"id": "", "ok": False, "error": "Invalid JSON"},
				status=400,
			)

		validation_error = _validate_command_payload(cmd)
		if validation_error:
			_write_command_audit(cmd, result="rejected", error=validation_error)
			return web.json_response(
				{"id": "", "ok": False, "error": validation_error},
				status=400,
			)

		cmd_id = cmd["id"]
		if capture_results.contains(cmd_id):
			_write_command_audit(cmd, result="rejected", error="Duplicate visible capture command id is retained for recovery")
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": "Duplicate visible capture command id is retained for recovery"},
				status=409,
			)
		# Take the socket and its hello state together before the first await.
		# Never read ext_ws again for this command: a replacement cannot receive a
		# command that was authorised for a different connection.
		target_ws = ext_ws
		if target_ws is None or target_ws.closed:
			_write_command_audit(cmd, result="rejected", error="Extension not connected")
			return web.json_response(
				{"id": "", "ok": False, "error": "Extension not connected"},
				status=503,
			)
		if _requires_radar_integrity(cmd):
			try:
				expected_version, expected_hash = _packaged_extension_identity()
			except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
				_write_command_audit(cmd, result="rejected", error=f"Radar extension integrity unavailable: {exc}")
				return web.json_response(
					{"id": cmd_id, "ok": False, "error": f"Radar extension integrity unavailable: {exc}"},
					status=503,
				)
			integrity_error = _radar_integrity_error(
				cmd, handshake, version=expected_version, build_hash=expected_hash,
			)
			if integrity_error:
				_write_command_audit(cmd, result="rejected", error=integrity_error)
				return web.json_response(
					{"id": cmd_id, "ok": False, "error": integrity_error},
					status=503,
				)
		if cmd["action"] in _VISIBLE_CAPTURE_ACTIONS:
			sequence_error = visible_capture_sequence.admit(cmd, target_ws)
			if sequence_error:
				_write_command_audit(cmd, result="rejected", error=sequence_error)
				return web.json_response({"id": cmd_id, "ok": False, "error": sequence_error}, status=409)

		if pending_commands.contains(cmd_id):
			_write_command_audit(cmd, result="rejected", error="Duplicate command id")
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": "Duplicate command id"},
				status=409,
			)
		last_activity = time.time()
		future = asyncio.get_event_loop().create_future()
		pending_commands.add(cmd_id, future, target_ws)
		if cmd["action"] in _VISIBLE_CAPTURE_ACTIONS and not capture_results.start(cmd_id, target_ws):
			pending_commands.remove(cmd_id)
			_write_command_audit(cmd, result="rejected", error="Duplicate visible capture command id is retained for recovery")
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": "Duplicate visible capture command id is retained for recovery"},
				status=409,
			)
		if cmd["action"] in _DIAGNOSED_PAGE_ACTIONS:
			inspect_diagnostics.start(cmd_id, target_ws, action=cmd["action"])

		try:
			_write_command_audit(cmd, result="accepted")
			await target_ws.send_json(cmd)
			result = await asyncio.wait_for(future, timeout=_COMMAND_TIMEOUT_SECONDS)
			_write_command_audit(
				cmd,
				result="ok" if isinstance(result, dict) and result.get("ok") is True else "error",
				error=result.get("error") if isinstance(result, dict) else "Invalid extension result",
			)
			return web.json_response(result)
		except asyncio.TimeoutError:
			if cmd["action"] in _DIAGNOSED_PAGE_ACTIONS:
				last_stage = inspect_diagnostics.record_timeout(cmd_id, target_ws)
				timeout_payload = {
					"id": cmd_id,
					"ok": False,
					"error": (
						f"{cmd['action']} timed out ({_COMMAND_TIMEOUT_SECONDS:g}s) after stage "
						f"{last_stage}; execution result unknown (browser-side execution was not cancelled)"
					),
				}
				if cmd["action"] in _VISIBLE_CAPTURE_ACTIONS:
					timeout_payload["recoveryAvailable"] = capture_results.contains(cmd_id)
				_write_command_audit(cmd, result="error", error=timeout_payload["error"])
				return web.json_response(
					timeout_payload,
					status=504,
				)
			timeout_error = f"Command timed out ({_COMMAND_TIMEOUT_SECONDS:g}s)"
			_write_command_audit(cmd, result="error", error=timeout_error)
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": timeout_error},
				status=504,
			)
		except Exception as e:
			if cmd["action"] in _VISIBLE_CAPTURE_ACTIONS:
				capture_results.record_failed(cmd_id, target_ws)
				visible_capture_sequence.record_failed(cmd_id, target_ws)
			send_error = f"Send failed: {e}"
			_write_command_audit(cmd, result="error", error=send_error)
			return web.json_response(
				{"id": cmd_id, "ok": False, "error": send_error},
				status=502,
			)
		finally:
			pending_commands.remove(cmd_id)

	# ── WebSocket handler（Chrome 扩展连接） ──────────────────────

	async def ws_handler(request: Any) -> Any:
		nonlocal ext_ws
		if not _is_extension_origin(request.headers.get("Origin")):
			raise web.HTTPForbidden(text="Chrome extension origin required")
		if ext_ws is not None and not ext_ws.closed:
			raise web.HTTPConflict(text="Bridge extension already connected")
		ws = web.WebSocketResponse()
		await ws.prepare(request)
		ext_ws = ws
		# A connection is not evidence of identity. Clear every identity field
		# before the new extension has supplied its hello message.
		handshake.begin_connection(request.headers.get("Origin"))
		print("[bridge] 扩展已连接", flush=True)

		try:
			async for msg in ws:
				if msg.type == web.WSMsgType.TEXT:
					data = json.loads(msg.data)
					msg_type = data.get("type")

					if msg_type == "hello":
						# An old handler can finish after a replacement was installed.
						# It must never overwrite the new socket's attestation.
						if ext_ws is ws:
							handshake.update_from_hello(data)
							print(f"[bridge] 扩展版本: {handshake.version}", flush=True)
						continue

					if msg_type == "log":
						level = data.get("level", "info")
						log_msg = data.get("msg", "")
						print(f"[bridge:ext:{level}] {log_msg}", flush=True)
						continue

					if msg_type == "inspect-diagnostic":
						# Telemetry is valid only for an existing probe sent on this
						# exact socket. Ignore spoofed IDs/stages and all other traffic.
						cmd_id = data.get("id")
						stage = data.get("stage")
						if isinstance(cmd_id, str) and isinstance(stage, str):
							inspect_diagnostics.record_stage(cmd_id, stage, ws)
						continue

					# 命令结果
					cmd_id = data.get("id")
					if isinstance(cmd_id, str):
						pending_commands.resolve_from(cmd_id, data, ws)
						inspect_diagnostics.record_result(cmd_id, data, ws)
						capture_results.record_result(cmd_id, data, ws)
						visible_capture_sequence.record_result(cmd_id, data, ws)
				elif msg.type == web.WSMsgType.ERROR:
					print(f"[bridge] WS error: {ws.exception()}", flush=True)
		finally:
			if ext_ws is ws:
				ext_ws = None
				handshake.reset()
				# Only cancel commands owned by this socket. A future replacement must
				# neither receive nor complete this connection's commands.
				pending_commands.disconnect(ws)
				inspect_diagnostics.record_disconnect(ws)
				capture_results.disconnect(ws)
				visible_capture_sequence.disconnect(ws)
			print("[bridge] 扩展已断开", flush=True)

		return ws

	# ── 启动统一服务 ──────────────────────────────────────────────

	app = web.Application(client_max_size=_MAX_COMMAND_TEXT)
	app.router.add_get(DAEMON_PING_PATH, handle_ping)
	app.router.add_get(DAEMON_STATUS_PATH, handle_status)
	app.router.add_post(DAEMON_COMMAND_PATH, handle_command)
	app.router.add_post(DAEMON_RESULT_PATH, handle_command_result)
	app.router.add_get(DAEMON_WS_PATH, ws_handler)

	runner = web.AppRunner(app, access_log=None)
	await runner.setup()
	site = web.TCPSite(runner, BRIDGE_HOST, BRIDGE_PORT)
	await site.start()
	print(f"[bridge] 服务启动: http://{BRIDGE_HOST}:{BRIDGE_PORT}", flush=True)
	print(f"[bridge] PID: {os.getpid()}, 空闲超时: {DAEMON_IDLE_TIMEOUT}s", flush=True)

	# 写 PID 文件（服务启动后再写，确保端口可用）
	_PID_FILE.write_text(str(os.getpid()))

	# ── 空闲超时检查 ──────────────────────────────────────────────

	try:
		while True:
			await asyncio.sleep(60)
			idle = time.time() - last_activity
			if idle > DAEMON_IDLE_TIMEOUT and ext_ws is None:
				print(f"[bridge] 空闲 {int(idle)}s 且无扩展连接，自动退出", flush=True)
				break
	except asyncio.CancelledError:
		pass
	finally:
		_PID_FILE.unlink(missing_ok=True)
		await runner.cleanup()
		print("[bridge] daemon 已停止", flush=True)


# ── CLI 入口：python -m boss_agent_cli.bridge.daemon --serve ────────

if __name__ == "__main__":
	if "--serve" in sys.argv:
		_ensure_dirs()
		try:
			asyncio.run(_run_daemon())
		except KeyboardInterrupt:
			pass
		finally:
			_PID_FILE.unlink(missing_ok=True)
