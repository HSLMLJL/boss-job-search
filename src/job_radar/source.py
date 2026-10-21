"""Single-browser-context source boundary for Job Radar."""

from __future__ import annotations

import hashlib
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from random import uniform
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode


class SourceError(RuntimeError):
	pass


class SourceAuthRequired(SourceError):
	pass


class SourceRiskStopped(SourceError):
	pass


class JobRadarSource(Protocol):
	is_demo: bool

	def open(self) -> None: ...
	def search(self, query: str, city: str, page: int, filters: dict[str, Any]) -> dict[str, Any]: ...
	def detail(self, job: dict[str, Any]) -> dict[str, Any]: ...
	def close(self) -> None: ...


def _first(raw: dict[str, Any], *names: str) -> str:
	for name in names:
		value = raw.get(name)
		if value is not None and str(value).strip():
			return str(value).strip()
	return ""


def _string_list(value: Any) -> list[str]:
	if not isinstance(value, (list, tuple)):
		return []
	return [str(item).strip() for item in value if str(item).strip()]


def _stable_key(raw: dict[str, Any]) -> str:
	material = _first(raw, "encryptJobId", "jobId", "job_id", "id")
	opaque_id = bool(material)
	if not material:
		material = "|".join(
			(
				_first(raw, "brandName", "company"),
				_first(raw, "jobName", "title"),
				_first(raw, "cityName", "city"),
				_first(raw, "areaDistrict", "district"),
			)
		)
	canonical = material if opaque_id else material.casefold()
	return "boss_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _lookup_label(value: str, lookup: dict[str, str]) -> str:
	return next((name for name, code in lookup.items() if str(code) == value), value) if value else ""


def _published_at(raw: dict[str, Any]) -> str:
	value = raw.get("jobPublishTime", raw.get("publishTime"))
	if value is None or value == "":
		return ""
	if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
		stamp = float(value)
		if stamp > 10_000_000_000:
			stamp /= 1000
		try:
			return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
		except (ValueError, OSError):
			return ""
	return str(value).strip()


def normalize_job(raw: dict[str, Any], *, fallback_city: str = "") -> dict[str, Any]:
	from boss_agent_cli.api import endpoints

	job_id = _first(raw, "encryptJobId", "jobId", "job_id", "id")
	industry = _first(raw, "industryName", "brandIndustryName", "industry", "brandIndustry")
	job_type = _first(raw, "jobTypeName", "job_type", "jobType")
	result = {
		"job_key": _stable_key(raw),
		"title": _first(raw, "jobName", "title"),
		"company": _first(raw, "brandName", "company"),
		"city": _first(raw, "cityName", "city") or fallback_city,
		"district": _first(raw, "areaDistrict", "district"),
		"address": _first(raw, "address", "businessDistrict", "location"),
		"salary": _first(raw, "salaryDesc", "salary"),
		"experience": _first(raw, "jobExperience", "experience"),
		"education": _first(raw, "jobDegree", "education"),
		"industry": _lookup_label(industry, endpoints.INDUSTRY_CODES),
		"scale": _first(raw, "brandScaleName", "scaleName", "scale"),
		"stage": _first(raw, "brandStageName", "stageName", "stage"),
		"job_type": _lookup_label(job_type, endpoints.JOB_TYPE_CODES),
		"welfare": _string_list(raw.get("welfareList") or raw.get("welfare")),
		"skills": _string_list(raw.get("skills") or raw.get("jobLabels")),
		"description": "",
		"list_summary": _first(raw, "postDescription", "jobDescription", "description"),
		"url": f"https://www.zhipin.com/job_detail/{quote(job_id, safe='~_-')}.html" if job_id else "",
		"detail_status": "not_requested",
		"_security_id": _first(raw, "securityId", "security_id"),
		"_job_id": job_id,
	}
	published = _published_at(raw)
	if published:
		result["published_at"] = published
	return result


def build_platform_params(filters: dict[str, Any]) -> dict[str, str]:
	from boss_agent_cli.search_filters import resolve_search_code_params

	supported = ("salary", "experience", "education", "industry", "scale", "stage", "job_type")
	return resolve_search_code_params(
		**{key: str(filters[key]) if filters.get(key) not in (None, "") else None for key in supported}
	)


class BossJobRadarSource:
	is_demo = False

	def __init__(
		self,
		*,
		data_dir: Path,
		cookies: dict[str, str],
		user_agent: str,
		cdp_port: int,
		min_delay: float = 2.0,
		max_delay: float = 5.0,
		sleeper: Callable[[float], None] = time.sleep,
		clock: Callable[[], float] = time.monotonic,
		delay_picker: Callable[[float, float], float] = uniform,
	) -> None:
		self._data_dir, self._cookies, self._user_agent, self._cdp_port = data_dir, cookies, user_agent, cdp_port
		self._session: Any | None = None
		self._risk_error: tuple[type[Exception], ...] = ()
		self._auth_error: tuple[type[Exception], ...] = ()
		self._min_delay, self._max_delay = min_delay, max_delay
		self._sleeper, self._clock, self._delay_picker = sleeper, clock, delay_picker
		self._last_request: float | None = None

	def open(self) -> None:
		if self._session is not None:
			return
		try:
			from boss_agent_cli.crawler.transport import CrawlAuthRequired, CrawlRiskError, DrissionCrawlerSession
		except ImportError as exc:
			raise SourceError("请运行 uv sync --extra crawl") from exc
		self._auth_error = (CrawlAuthRequired,)
		self._risk_error = (CrawlRiskError,)
		try:
			self._session = DrissionCrawlerSession(
				profile_path=self._data_dir / "job-radar" / "chrome-profile",
				chrome_path=None,
				cdp_port=self._cdp_port,
				hook_profile="none",
				hook_dir=None,
				seed_cookies=self._cookies,
				user_agent=self._user_agent,
			)
			self._session.open()
		except CrawlAuthRequired as exc:
			raise SourceAuthRequired("采集浏览器当前未登录") from exc
		except CrawlRiskError as exc:
			raise SourceRiskStopped("浏览器会话被平台风险拦截") from exc
		except Exception as exc:
			raise SourceError("无法打开受控浏览器") from exc

	def _wait(self) -> None:
		if self._last_request is not None:
			self._sleeper(
				max(0.0, self._delay_picker(self._min_delay, self._max_delay) - (self._clock() - self._last_request))
			)
		self._last_request = self._clock()

	def _xhr(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
		if self._session is None or getattr(self._session, "_page", None) is None:
			raise SourceError("browser context is unavailable")
		self._wait()
		script = """function(path, params) { const q=new URLSearchParams(); Object.entries(params).forEach(([k,v])=>{if(v!==null&&v!==undefined&&String(v)!=='')q.set(k,String(v));}); const x=new XMLHttpRequest(); try{x.open('GET',path+'?'+q.toString(),false);x.withCredentials=true;x.setRequestHeader('Accept','application/json, text/plain, */*');x.send(null);return {status:x.status,text:x.responseText||''};}catch(e){return {status:0,error:true};}}"""
		try:
			raw = self._session._page.run_js(script, path, params)
		except Exception as exc:
			raise SourceError("浏览器上下文请求失败") from exc
		if isinstance(raw, str):
			try:
				raw = json.loads(raw)
			except json.JSONDecodeError as exc:
				raise SourceError("浏览器响应无法解析") from exc
		if not isinstance(raw, dict) or raw.get("error") or int(raw.get("status") or 0) <= 0:
			raise SourceError("浏览器上下文请求失败")
		status = int(raw["status"])
		if status == 401:
			raise SourceAuthRequired("平台登录已失效")
		if status in (403, 429):
			raise SourceRiskStopped("平台风险或频率限制拦截")
		if status >= 400:
			raise SourceError("浏览器上下文请求失败")
		try:
			payload = json.loads(str(raw.get("text") or ""))
		except json.JSONDecodeError as exc:
			raise SourceError("浏览器响应不是 JSON") from exc
		if not isinstance(payload, dict):
			raise SourceError("浏览器响应不是对象")
		return payload

	def _raise_for_payload(self, payload: dict[str, Any]) -> None:
		if "code" not in payload:
			raise SourceError("平台响应缺少状态码")
		code = payload["code"]
		if code in (0, "0"):
			return
		if code in (32, "32", 36, "36", 37, "37", 38, "38"):
			raise SourceRiskStopped(f"平台风险或验证拦截（code {code}）")
		raise SourceError(f"平台请求失败（code {code}）")

	def search(self, query: str, city: str, page: int, filters: dict[str, Any]) -> dict[str, Any]:
		if self._session is None:
			raise SourceError("source is not open")
		try:
			from boss_agent_cli.api.endpoints import CITY_CODES

			city_code = str(CITY_CODES.get(city, city))
			params = {"query": query, "city": city_code, "page": page, **build_platform_params(filters)}
			self._session._page.get("https://www.zhipin.com/web/geek/jobs?" + urlencode(params))
			self._session._raise_if_security_page()
			payload = self._xhr("/wapi/zpgeek/search/joblist.json", params)
			self._raise_for_payload(payload)
		except self._auth_error as exc:
			raise SourceAuthRequired("采集浏览器当前未登录") from exc
		except self._risk_error as exc:
			raise SourceRiskStopped("平台风险或验证拦截") from exc
		except SourceError:
			raise
		except Exception as exc:
			raise SourceError("职位搜索请求失败") from exc
		data = payload.get("zpData") or payload.get("data")
		if not isinstance(data, dict) or not isinstance(data.get("jobList"), list):
			raise SourceError("职位搜索响应缺少岗位列表")
		has_more = data.get("hasMore")
		if not isinstance(has_more, (bool, int)) or has_more not in (True, False, 0, 1):
			raise SourceError("职位搜索响应缺少分页状态")
		return {
			"jobs": [normalize_job(card, fallback_city=city) for card in data["jobList"] if isinstance(card, dict)],
			"has_more": bool(has_more),
		}

	def detail(self, job: dict[str, Any]) -> dict[str, Any]:
		security_id = str(job.get("_security_id") or "")
		if not security_id:
			raise SourceError("岗位缺少详情标识，无法请求完整 JD")
		try:
			if self._session is None:
				raise SourceError("source is not open")
			self._session._raise_if_security_page()
			payload = self._xhr("/wapi/zpgeek/job/card.json", {"securityId": security_id})
			self._raise_for_payload(payload)
		except self._auth_error as exc:
			raise SourceAuthRequired("采集浏览器当前未登录") from exc
		except self._risk_error as exc:
			raise SourceRiskStopped("平台风险或验证拦截") from exc
		except SourceError:
			raise
		except Exception as exc:
			raise SourceError("职位详情请求失败") from exc
		data = payload.get("zpData") or payload.get("data")
		if not isinstance(data, dict):
			raise SourceError("职位详情响应缺少数据对象")
		card = data.get("jobCard") if isinstance(data.get("jobCard"), dict) else data
		description = _first(card, "postDescription", "jobDescription", "description")
		result: dict[str, Any] = {"detail_status": "fetched" if description else "empty"}
		if description:
			result["description"] = description
		for name, value in (
			("skills", _string_list(card.get("skills") or card.get("jobLabels"))),
			("welfare", _string_list(card.get("welfareList") or card.get("welfare"))),
		):
			if value:
				result[name] = value
		address = _first(card, "address", "businessDistrict", "location")
		if address:
			result["address"] = address
		return result

	def close(self) -> None:
		if self._session is not None:
			try:
				self._session.close()
			finally:
				self._session = None


def _unused_local_port() -> int:
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
		connection.bind(("127.0.0.1", 0))
		return int(connection.getsockname()[1])


def make_source(data_dir: Path | str) -> JobRadarSource:
	"""Use the user's normal Chrome through the packaged, verified extension."""
	from job_radar.bridge_source import BridgeJobRadarSource

	return BridgeJobRadarSource()
