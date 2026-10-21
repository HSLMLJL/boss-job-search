"""Browser Bridge 协议定义 — daemon / 扩展 / CLI 三方共享。"""

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

# ── 端口配置 ──────────────────────────────────────────────────────────

DEFAULT_PORT = 19826
BRIDGE_PORT = int(os.getenv("BOSS_BRIDGE_PORT", str(DEFAULT_PORT)))
BRIDGE_HOST = "127.0.0.1"
DAEMON_WS_PATH = "/ext"
DAEMON_PING_PATH = "/ping"
DAEMON_STATUS_PATH = "/status"
DAEMON_COMMAND_PATH = "/command"
DAEMON_RESULT_PATH = "/command-result"

# daemon 空闲超时：4 小时
DAEMON_IDLE_TIMEOUT = int(os.getenv("BOSS_BRIDGE_TIMEOUT", str(4 * 3600)))


def is_radar_search_url(value: str) -> bool:
	"""Limit the fixed visible-page operation to an unambiguous BOSS search."""
	if not isinstance(value, str) or len(value) > 2048:
		return False
	try:
		parsed = urlparse(value)
		host = (parsed.hostname or "").lower()
		query = parse_qs(parsed.query, keep_blank_values=True)
		trusted_origin = (
			parsed.scheme == "https"
			and (host == "zhipin.com" or host.endswith(".zhipin.com"))
			and parsed.port in (None, 443)
			and parsed.username is None and parsed.password is None
		)
		if not trusted_origin or len(query.get("query", [])) != 1 or not query["query"][0].strip():
			return False
		# BOSS exposes both search routes; a city route may redirect to the SPA.
		# Route availability is checked live, not inferred from a release date.
		return (
			(
				parsed.path == "/web/geek/jobs"
				and len(query.get("city", [])) == 1
				and bool(query["city"][0].strip())
			)
			or bool(re.fullmatch(r"/c\d{9}/?", parsed.path))
		)
	except (TypeError, ValueError):
		return False


def validate_visible_adoption(data: Any, *, tab_id: int, url: str, page: int) -> str | None:
	"""Validate an atomic read of an already-loaded cumulative list, not a resume claim."""
	if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0:
		return "Invalid visible adoption envelope"
	proof = data.get("adoption")
	zp = data.get("zpData")
	if not isinstance(proof, dict) or not isinstance(zp, dict):
		return "Missing visible adoption evidence"
	if (type(proof.get("tabId")) is not int or proof["tabId"] != tab_id
		or proof.get("url") != url or proof.get("readOnly") is not True
		or type(proof.get("currentPage")) is not int or proof["currentPage"] != page
		or type(zp.get("currentPage")) is not int or zp["currentPage"] != page):
		return "Visible adoption identity mismatch"
	size = zp.get("pageSize")
	batches = zp.get("batches")
	if type(size) is not int or not 1 <= size <= 100 or not isinstance(batches, list) or len(batches) != page:
		return "Invalid visible adoption batch boundaries"
	count = 0
	for number, batch in enumerate(batches, 1):
		if not isinstance(batch, dict) or type(batch.get("page")) is not int or batch["page"] != number:
			return "Non-contiguous visible adoption batches"
		items, state, more = batch.get("jobList"), batch.get("pageState"), batch.get("hasMore")
		if not isinstance(items, list) or type(more) is not bool or not isinstance(state, dict):
			return "Invalid visible adoption batch"
		if (type(state.get("currentPage")) is not int or state["currentPage"] != number
			or type(state.get("pageSize")) is not int or state["pageSize"] != size
			or state.get("identityMatched") is not True or state.get("hasMore") is not more
			or (not more and state.get("terminal") is not True)):
			return "Invalid visible adoption page evidence"
		if len(items) > size or ((number < page or more) and len(items) != size) or (number < page and not more):
			return "Incomplete cumulative visible adoption list"
		if any(not isinstance(item, dict) or not isinstance(item.get("encryptJobId"), str)
			or not 1 <= len(item["encryptJobId"]) <= 160 for item in items):
			return "Visible adoption job has no stable ID"
		count += len(items)
	if (type(zp.get("totalCount")) is not int or zp["totalCount"] != count
		or type(proof.get("jobListCount")) is not int or proof["jobListCount"] != count):
		return "Visible adoption count mismatch"
	return None


# ── 命令类型 ──────────────────────────────────────────────────────────

@dataclass
class BridgeCommand:
	"""CLI → daemon → 扩展 的命令。"""
	id: str
	action: str  # exec | navigate | capture-visible-jobs | inspect-radar-page | present-radar-tab | legacy capture-job-list | radar-tab-status | close-window
	code: str = ""
	url: str = ""
	workspace: str = "boss"
	tab_id: int | None = None
	page: int | None = None
	start_fresh: bool = False
	allow_background_continuation: bool = False
	panel_mode: str = ""
	expected_job_id: str = ""

	def to_dict(self) -> dict[str, Any]:
		d: dict[str, Any] = {"id": self.id, "action": self.action}
		if self.code:
			d["code"] = self.code
		if self.url:
			d["url"] = self.url
		if self.workspace:
			d["workspace"] = self.workspace
		if self.tab_id is not None:
			d["tabId"] = self.tab_id
		if self.page is not None:
			d["page"] = self.page
		if self.action == "capture-current-job-cards" or self.start_fresh:
			d["startFresh"] = self.start_fresh
		if self.allow_background_continuation:
			d["allowBackgroundContinuation"] = True
		if self.panel_mode:
			d["panelMode"] = self.panel_mode
		if self.expected_job_id:
			d["expectedJobId"] = self.expected_job_id
		return d


@dataclass
class BridgeResult:
	"""扩展 → daemon → CLI 的结果。"""
	id: str
	ok: bool
	data: Any = None
	error: str = ""

	@classmethod
	def from_dict(cls, d: dict[str, Any]) -> "BridgeResult":
		return cls(
			id=d.get("id", ""),
			ok=d.get("ok", False),
			data=d.get("data"),
			error=d.get("error", ""),
		)


def make_command_id() -> str:
	"""生成唯一命令 ID（uuid 防碰撞）。"""
	import uuid
	return f"cmd_{uuid.uuid4().hex[:12]}"
