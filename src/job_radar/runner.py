"""Resumable source-neutral Job Radar execution."""

from __future__ import annotations
from copy import deepcopy
import time
from typing import Any, Callable
from job_radar.config import TaskConfig
from job_radar.source import SourceAuthRequired, SourceError, SourceRiskStopped

ProgressCallback = Callable[[dict[str, Any]], None]


def _pair_key(k: str, c: str) -> str:
	return f"{k}\u241f{c}"


def _safe(e: BaseException) -> str:
	return (str(e).replace("\n", " ").strip() or type(e).__name__)[:500]


def _counts(value: Any) -> dict[str, int]:
	keys = ("requests", "pages", "listed", "unique", "matched", "details_requested", "details_fetched", "details_empty")
	return {
		k: value.get(k, 0)
		if isinstance(value, dict) and isinstance(value.get(k, 0), int) and value.get(k, 0) >= 0
		else 0
		for k in keys
	}


def _checkpoint(value: Any) -> dict[str, Any]:
	value = value if isinstance(value, dict) else {}
	return {
		"pairs": deepcopy(value.get("pairs") if isinstance(value.get("pairs"), dict) else {}),
		"pending_details": [k for k in value.get("pending_details", []) if isinstance(k, str)],
		"pending_rows": [r for r in value.get("pending_rows", []) if isinstance(r, dict)],
		"current": deepcopy(value.get("current") if isinstance(value.get("current"), dict) else {}),
	}


def _stop(event: Any) -> bool:
	return bool(event is not None and callable(getattr(event, "is_set", None)) and event.is_set())


def _evaluate(job: dict[str, Any], filters: dict[str, Any], pending: bool) -> tuple[bool, str]:
	from job_radar.filters import evaluate_job

	return evaluate_job(job, filters, detail_pending=pending)


def _clean(job: dict[str, Any]) -> dict[str, Any]:
	result = dict(job)
	result["description"] = ""
	result["detail_status"] = "not_requested"
	return result


def run_task(
	config: TaskConfig,
	store: Any,
	source: Any,
	*,
	run_id: str | None = None,
	stop_event: Any | None = None,
	on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
	run = (
		store.create_run(config.to_dict(), demo=bool(getattr(source, "is_demo", False)))
		if run_id is None
		else store.get_run(run_id)
	)
	if run is None:
		raise KeyError(run_id)
	run_id = str(run["id"])
	counts = _counts(run.get("counts"))
	checkpoint = _checkpoint(run.get("checkpoint"))
	started = time.monotonic()
	window = {"requests": 0, "details": 0, "jobs": 0}
	pair_pages: dict[str, int] = {}

	def save(status: str, message: str, finished: bool = False) -> dict[str, Any]:
		jobs = store.get_jobs(run_id)
		counts["unique"] = len({str(j.get("job_key")) for j in jobs if j.get("job_key")})
		counts["matched"] = sum(bool(j.get("matched")) for j in jobs)
		counts["pairs_visited"] = len(checkpoint["pairs"])
		counts["pairs_completed"] = sum(bool(pair.get("complete")) for pair in checkpoint["pairs"].values())
		update = {"status": status, "counts": counts, "checkpoint": checkpoint, "message": message}
		if finished:
			update["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
		saved = store.update_run(run_id, **update)
		if on_progress:
			on_progress(
				{
					"run_id": run_id,
					"status": status,
					"counts": deepcopy(counts),
					"checkpoint": deepcopy(checkpoint),
					"message": message,
				}
			)
		return saved

	def limit(kind: str) -> str | None:
		if time.monotonic() - started >= config.limits["seconds"]:
			return "运行时间预算已用尽"
		if kind in {"list", "detail"} and window["requests"] >= config.limits["requests"]:
			return "列表与详情共用请求预算已用尽"
		if kind == "detail" and window["details"] >= config.limits["details"]:
			return "JD 详情预算已用尽"
		if kind == "job" and window["jobs"] >= config.limits["jobs"]:
			return "岗位数量预算已用尽"
		return None

	def queue(job: dict[str, Any]) -> None:
		if job["job_key"] not in checkpoint["pending_details"]:
			checkpoint["pending_details"].append(job["job_key"])

	def process(raw: dict[str, Any], keyword: str) -> None:
		window["jobs"] += 1
		job = dict(raw)
		job["queries"] = list(dict.fromkeys([*job.get("queries", []), keyword]))
		existing = store.get_job(run_id, str(job["job_key"]))
		if existing:
			job["queries"] = list(dict.fromkeys([*existing.get("queries", []), *job["queries"]]))
			if existing.get("detail_status") in {"fetched", "empty"}:
				for field in ("description", "skills", "welfare", "address", "detail_status"):
					job[field] = existing.get(field, job.get(field))
		if config.details == "none":
			job = _clean(job)
			job["matched"], job["filter_reason"] = _evaluate(job, config.filters, False)
			store.upsert_job(run_id, job)
			return
		if job.get("detail_status") in {"fetched", "empty"}:
			job["matched"], job["filter_reason"] = _evaluate(job, config.filters, False)
			store.upsert_job(run_id, job)
			return
		job = _clean(job)
		if config.details == "matched":
			eligible, _ = _evaluate(job, config.filters, True)
			job["matched"], job["filter_reason"] = _evaluate(job, config.filters, False)
			job["detail_status"] = "pending" if eligible else "skipped"
		else:
			eligible = True
			job["matched"], job["filter_reason"] = False, "等待完整 JD 后筛选"
			job["detail_status"] = "pending"
		stored = store.upsert_job(run_id, job)
		if eligible:
			queue(stored)

	def drain_details() -> str | None:
		while checkpoint["pending_details"]:
			if _stop(stop_event):
				return "用户已请求停止"
			key = checkpoint["pending_details"][0]
			job = store.get_job(run_id, key)
			if job is None or job.get("detail_status") in {"fetched", "empty"}:
				checkpoint["pending_details"].pop(0)
				continue
			why = limit("detail")
			if why:
				return why
			counts["requests"] += 1
			counts["details_requested"] += 1
			window["requests"] += 1
			window["details"] += 1
			detail = source.detail(job)
			merged = dict(job)
			for field in ("description", "skills", "welfare", "address", "detail_status"):
				if field in detail:
					merged[field] = detail[field]
			if not str(merged.get("description") or "").strip():
				merged["detail_status"] = "empty"
				counts["details_empty"] += 1
			else:
				merged["detail_status"] = "fetched"
				counts["details_fetched"] += 1
			merged["matched"], merged["filter_reason"] = _evaluate(merged, config.filters, False)
			store.upsert_job(run_id, merged)
			checkpoint["pending_details"].pop(0)
			save("running", "已补全并复核一条 JD")
		return None

	def consume_rows() -> str | None:
		while checkpoint["pending_rows"]:
			if _stop(stop_event):
				return "用户已请求停止"
			why = limit("job")
			if why:
				return why
			item = checkpoint["pending_rows"].pop(0)
			raw = item.get("job")
			if isinstance(raw, dict) and raw.get("job_key"):
				process(raw, str(item.get("keyword") or ""))
				save("running", "已保存一条岗位")
		return None

	def process_page() -> str | None:
		row_limit = consume_rows()
		# A job cap prevents adding more rows, but saved jobs still have their
		# separately budgeted JD work. Stop/time/request limits remain effective.
		if row_limit is None or row_limit == "岗位数量预算已用尽":
			return drain_details() or row_limit
		return row_limit

	try:
		save("running", "正在连接 Chrome 搜索页面")
		source.open()
		why = process_page()
		if why:
			return save("budget_stopped" if "预算" in why else "stopped", why, True)
		for keyword in config.keywords:
			for city in config.cities:
				pk = _pair_key(keyword, city)
				pair = checkpoint["pairs"].get(pk, {"next_page": 1, "complete": False})
				if pair.get("complete"):
					continue
				while pair_pages.get(pk, 0) < config.limits["pages"]:
					if _stop(stop_event):
						return save("stopped", "用户已请求停止；已保留页面断点", True)
					why = limit("list") or ("岗位数量预算已用尽" if window["jobs"] >= config.limits["jobs"] else None)
					if why:
						return save("budget_stopped", why, True)
					page = max(1, int(pair.get("next_page", 1)))
					checkpoint["current"] = {"keyword": keyword, "city": city, "page": page, "phase": "list"}
					save("running", f"正在读取 {city} / {keyword} 第 {page} 页")
					counts["requests"] += 1
					window["requests"] += 1
					response = source.search(keyword, city, page, config.filters)
					jobs = response.get("jobs")
					if not isinstance(jobs, list):
						raise SourceError("来源未返回岗位列表")
					has_more = bool(response.get("has_more", False))
					counts["pages"] += 1
					counts["listed"] += len(jobs)
					pair_pages[pk] = pair_pages.get(pk, 0) + 1
					pair = {"next_page": page + 1, "complete": not has_more, "partial": False}
					checkpoint["pairs"][pk] = pair
					checkpoint["pending_rows"].extend(
						{"keyword": keyword, "job": job} for job in jobs if isinstance(job, dict)
					)
					checkpoint["current"] = {}
					save("running", f"已保存 {city} / {keyword} 第 {page} 页断点")
					why = process_page()
					if why:
						return save("budget_stopped" if "预算" in why else "stopped", why, True)
					if not has_more:
						break
				if not pair.get("complete") and pair_pages.get(pk, 0) >= config.limits["pages"]:
					pair["partial"] = True
					pair["reason"] = "page_limit_reached_platform_has_more"
					checkpoint["pairs"][pk] = pair
					save("running", f"{city} / {keyword} 已到本次页数窗口，继续可从第 {pair['next_page']} 页开始")
		checkpoint["current"] = {}
		incomplete = any(
			not bool(checkpoint["pairs"].get(_pair_key(k, c), {}).get("complete"))
			for k in config.keywords
			for c in config.cities
		)
		return save(
			"partial" if incomplete else "completed",
			"本次页数窗口已结束；可继续读取下一页" if incomplete else "任务完成",
			True,
		)
	except SourceRiskStopped as exc:
		return save("risk_stopped", _safe(exc), True)
	except SourceAuthRequired as exc:
		return save("auth_required", _safe(exc), True)
	except SourceError as exc:
		return save("stopped", _safe(exc), True)
	except Exception as exc:
		return save("failed", _safe(exc), True)
	finally:
		try:
			source.close()
		except Exception:
			pass
