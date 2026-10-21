"""Normal-Chrome Browser Bridge source; fixed extension actions only."""

from __future__ import annotations
import time
from typing import Any, Callable
from urllib.parse import urlencode
from job_radar.source import SourceAuthRequired, SourceError, SourceRiskStopped, normalize_job


class BridgeJobRadarSource:
	is_demo = False

	def __init__(
		self,
		*,
		bridge: Any | None = None,
		extension_status_reader: Callable[[], dict[str, Any]] | None = None,
		sleeper: Callable[[float], None] = time.sleep,
	) -> None:
		self._bridge = bridge
		self._extension_status_reader = extension_status_reader
		self._sleeper = sleeper
		self._jobs: dict[str, dict[str, Any]] = {}

	def open(self) -> None:
		if self._bridge is None:
			try:
				from boss_agent_cli.bridge.client import BridgeClient

				self._bridge = BridgeClient(max_retries=1)
			except Exception as exc:
				raise SourceError("本地 Browser Bridge 不可用") from exc
		if self._extension_status_reader is None:
			try:
				from job_radar.onboarding import extension_data

				self._extension_status_reader = extension_data
			except Exception as exc:
				raise SourceError("无法读取 Bridge 扩展完整性") from exc
		try:
			status = self._extension_status_reader()
		except Exception as exc:
			raise SourceError("无法读取 Bridge 扩展完整性") from exc
		if not isinstance(status, dict) or status.get("verified") is not True:
			raise SourceError("Browser Bridge 扩展未完成版本和指纹核验")

	@staticmethod
	def _url(query: str, city: str) -> str:
		if not city.isdigit():
			raise SourceError("Bridge 搜索参数无效")
		return "https://www.zhipin.com/web/geek/jobs?" + urlencode({"city": city, "query": query})

	@staticmethod
	def _raise_bridge_error(error: Any) -> None:
		text = str(error or "").lower()
		if any(
			x in text for x in ("captcha", "verification", "risk", "环境存在异常", "安全验证", "code=37", "code=38")
		):
			raise SourceRiskStopped("Bridge 检测到平台风险或验证")
		if any(x in text for x in ("login", "登录", "未登录", "扫码")):
			raise SourceAuthRequired("正常 Chrome 登录状态不可用")
		raise SourceError("Bridge 页面采集失败")

	@staticmethod
	def _data(result: Any) -> dict[str, Any]:
		if getattr(result, "ok", False) is not True:
			BridgeJobRadarSource._raise_bridge_error(getattr(result, "error", ""))
		if not isinstance(getattr(result, "data", None), dict):
			raise SourceError("Bridge 返回数据无效")
		return result.data

	def _tab(self, url: str) -> int:
		try:
			state = self._bridge.radar_tab_status()
		except Exception as exc:
			raise SourceError("无法确认 Bridge 搜索标签页") from exc
		if (
			not isinstance(state, dict)
			or state.get("prepared") is not True
			or state.get("url") != url
			or type(state.get("tabId")) is not int
			or state["tabId"] <= 0
		):
			raise SourceError("Bridge 未绑定精确的搜索标签页")
		return state["tabId"]

	def search(self, query: str, city: str, page: int, filters: dict[str, Any]) -> dict[str, Any]:
		del filters
		from boss_agent_cli.api.endpoints import CITY_CODES

		code = str(CITY_CODES.get(city, city))
		url = self._url(query, code)
		try:
			data = self._bridge.capture_job_list(url, page=page, workspace="boss-radar", start_fresh=page == 1)
		except Exception as exc:
			self._raise_bridge_error(exc)
		if not isinstance(data, dict) or data.get("code") != 0:
			if isinstance(data, dict) and data.get("code") in (37, 38):
				raise SourceRiskStopped("Bridge 检测到平台风险或验证")
			raise SourceError("Bridge 列表响应状态无效")
		zp = data.get("zpData")
		state = zp.get("pageState") if isinstance(zp, dict) else None
		if (
			not isinstance(zp, dict)
			or not isinstance(zp.get("jobList"), list)
			or type(zp.get("hasMore")) is not bool
			or not isinstance(state, dict)
		):
			raise SourceError("Bridge 列表响应缺少职位或分页状态")
		if (
			state.get("currentPage") != page
			or state.get("identityMatched") is not True
			or type(state.get("pageSize")) is not int
			or state["pageSize"] != len(zp["jobList"])
			or state.get("hasMore") is not zp["hasMore"]
			or state.get("terminal") is not (not zp["hasMore"])
		):
			raise SourceError("Bridge 列表页面身份或分页证据未验证")
		info = {"_bridge_tab": self._tab(url), "_bridge_url": url}
		jobs = [normalize_job(item, fallback_city=city) for item in zp["jobList"] if isinstance(item, dict)]
		for job in jobs:
			job.update(info)
			self._jobs[str(job["job_key"])] = dict(info)
		return {"jobs": jobs, "has_more": zp["hasMore"]}

	def _panel(self, tab: int, url: str, mode: str, job_id: str = "") -> dict[str, Any]:
		return self._data(
			self._bridge.send_command(
				"read-search-job-panel",
				allow_replay=False,
				workspace="boss-radar",
				tab_id=tab,
				url=url,
				panel_mode=mode,
				expected_job_id=job_id,
			)
		)

	def detail(self, job: dict[str, Any]) -> dict[str, Any]:
		info = {"_bridge_tab": job.get("_bridge_tab"), "_bridge_url": job.get("_bridge_url")}
		info = {**self._jobs.get(str(job.get("job_key") or ""), {}), **{k: v for k, v in info.items() if v}}
		tab, url, job_id = info.get("_bridge_tab"), info.get("_bridge_url"), str(job.get("_job_id") or "")
		if type(tab) is not int or not isinstance(url, str) or not job_id:
			raise SourceError("完整 JD 需要仍绑定原搜索页面")
		inspect = self._panel(tab, url, "inspect")
		cards = inspect.get("cards") if inspect.get("status") == "ready" and inspect.get("href") == url else None
		if not isinstance(cards, list) or job_id not in {
			str(x.get("stable_job_id") or "") for x in cards if isinstance(x, dict)
		}:
			raise SourceError("当前页面未证明该岗位可读取详情")
		selected = self._panel(tab, url, "select", job_id)
		if (
			selected.get("status") != "selected"
			or selected.get("href") != url
			or str(selected.get("selected_job_id") or "") != job_id
		):
			raise SourceError("详情面板选择未验证")
		data: dict[str, Any] | None = None
		for attempt in range(3):
			candidate = self._panel(tab, url, "read", job_id)
			if candidate.get("status") == "ready" and candidate.get("href") == url:
				data = candidate
				break
			if attempt < 2:
				self._sleeper(0.5)
		if data is None:
			raise SourceError("详情面板未就绪")
		evidence = data.get("identity_evidence")
		if (
			str(data.get("stable_job_id") or "") != job_id
			or not isinstance(evidence, dict)
			or str(evidence.get("stable_job_id") or "") != job_id
			or evidence.get("exact_url") != url
			or not str(evidence.get("detail_url") or "")
		):
			raise SourceError("详情面板身份未验证")
		text = str(data.get("jd_text") or "").strip()
		if not text:
			return {"detail_status": "empty"}
		return {"description": text, "address": str(data.get("job_work_address") or ""), "detail_status": "fetched"}

	def close(self) -> None:
		return None


def make_bridge_source() -> BridgeJobRadarSource:
	return BridgeJobRadarSource()
