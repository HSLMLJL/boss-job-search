from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable

from boss_agent_cli.radar.models import RadarEvent, ai_relevance_evidence
from boss_agent_cli.radar.profile import RadarProfile
from boss_agent_cli.radar.store import RadarStore


class RadarReviewSampleError(RuntimeError):
	"""Raised when a local review sample cannot be formed truthfully."""


_SPECS: dict[str, dict[str, Any]] = {
	"relevance": {
		"label": "相关性抽检",
		"default_count": 20,
		"instruction": "逐条判断是否确为 AI 相关的产品岗位；相关记 true，误收记 false。",
	},
	"old_newness": {
		"label": "新增真实性抽检",
		"default_count": 50,
		"instruction": "逐条判断该岗位在本轮前是否已存在；确为新岗位记 true，旧岗位却报新增记 false。",
	},
}


def build_review_sample(
	store: RadarStore,
	profile: RadarProfile,
	*,
	metric: str,
	count: int | None = None,
) -> dict[str, Any]:
	"""Prepare a local-only, city-balanced sample for a human acceptance review.

	The sample deliberately has no browser or platform dependency. ``recall`` is
	excluded because a valid recall sample needs an independently gathered source,
	not listings already returned by the radar.
	"""
	if metric not in _SPECS:
		raise ValueError(f"unsupported review metric: {metric}")
	spec = _SPECS[metric]
	requested = int(count if count is not None else spec["default_count"])
	if requested < 1 or requested > 100:
		raise ValueError("count must be between 1 and 100")
	sessions = store.recent_completed_scan_sessions(
		profile_fingerprint=profile.fingerprint,
		limit=1,
	)
	if not sessions:
		raise RadarReviewSampleError("没有可用于抽检的完整 daily 扫描")
	session = sessions[0]
	run_ids = tuple(str(value) for value in session["run_ids"])
	if metric == "relevance":
		candidates = [RadarEvent(event_type="observed", job=job) for job in store.list_jobs_for_runs(run_ids)]
	else:
		candidates = [
			event
			for event in store.list_events_for_runs(run_ids)
			if event.event_type == "new"
		]
	if not candidates:
		raise RadarReviewSampleError(f"最近完整扫描没有可用于{spec['label']}的岗位")
	sample = _city_balanced_sample(candidates, requested)
	return {
		"profile": profile.name,
		"metric": metric,
		"metric_label": spec["label"],
		"instruction": spec["instruction"],
		"requested_count": requested,
		"available_count": len(candidates),
		"sample_size": len(sample),
		"minimum_sample": int(spec["default_count"]),
		"logical_scan_id": session["logical_scan_id"],
		"run_ids": run_ids,
		"platform_requests": 0,
		"jobs": [_sample_row(index, event) for index, event in enumerate(sample, start=1)],
		"note": (
			"样本按城市轮换选取，只读取本地数据库；不会打开链接、导航浏览器或请求平台。"
			"抽检结果应由本人确认后再用 record-review 汇总记录。"
		),
	}


def _city_balanced_sample(events: Iterable[RadarEvent], count: int) -> list[RadarEvent]:
	buckets: dict[str, deque[RadarEvent]] = defaultdict(deque)
	for event in sorted(
		events,
		key=lambda item: (
			item.job.city,
			item.job.title.casefold(),
			item.job.company.casefold(),
			item.job.platform_job_id,
		),
	):
		buckets[event.job.city or "未提供城市"].append(event)
	result: list[RadarEvent] = []
	while buckets and len(result) < count:
		for city in tuple(sorted(buckets)):
			if len(result) >= count:
				break
			bucket = buckets[city]
			result.append(bucket.popleft())
			if not bucket:
				del buckets[city]
	return result


def _sample_row(index: int, event: RadarEvent) -> dict[str, Any]:
	job = event.job
	return {
		"sample_no": index,
		"event_type": event.event_type,
		"title": job.title,
		"company": job.company,
		"city": job.city,
		"district": job.district,
		"salary": job.salary,
		"experience": job.experience,
		"education": job.education,
		"ai_evidence": list(ai_relevance_evidence(job)),
		"first_discovered_at": job.observed_at.isoformat(),
		"published_at": job.published_at.isoformat() if job.published_at else None,
		"url": job.job_url,
	}
