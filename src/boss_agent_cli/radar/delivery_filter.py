"""Pure, conservative post-merge delivery filtering."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any, Mapping

from boss_agent_cli.radar.models import JobObservation, radar_job_exclusion_reasons


def filter_delivery_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep-copied bundle with explicit internship/pay exclusions.

    Anchor annotations are removed because they count and annotate the original
    job set.  A caller may safely enrich the returned bundle with an anchor again.
    """
    result = deepcopy(dict(bundle))
    jobs = result.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("delivery bundle must contain a jobs list")
    kept: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    unknown_employment_type = 0
    for index, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            raise ValueError(f"delivery bundle jobs[{index}] must be an object")
        raw_employment_type = job.get("employment_type", "")
        employment_type = "" if raw_employment_type is None else raw_employment_type
        if employment_type not in ("", "实习"):
            raise ValueError(f"delivery bundle jobs[{index}].employment_type is not normalized")
        job["employment_type"] = employment_type
        if employment_type == "":
            unknown_employment_type += 1
        job_reasons = radar_job_exclusion_reasons(
            _observation(job), exclude_internships=True, exclude_hourly_daily_pay=True
        )
        if job_reasons:
            reasons.update(job_reasons)
            continue
        _remove_anchor_annotations(job)
        kept.append(job)
    result["jobs"] = kept
    result["counts"] = _counts(kept)
    anchor_removed = result.pop("history_anchor", None) is not None
    result["exclusion_summary"] = {
        "removed": len(jobs) - len(kept),
        "reasons": dict(sorted(reasons.items())),
        "unknown_employment_type": unknown_employment_type,
        "limitation": (
            "缺少规范化 employment_type/jobType 的历史记录不会补造岗位类型；"
            "该未知值仅能继续按标题和薪资字段执行既有排除规则。"
        ),
        "history_anchor_annotations_removed": anchor_removed,
    }
    return result


def _observation(job: Mapping[str, Any]) -> JobObservation:
    return JobObservation(
        platform=str(job.get("platform") or "zhipin"), query="", shard=str(job.get("city") or ""),
        title=str(job.get("title") or ""), company=str(job.get("company") or ""),
        city=str(job.get("city") or ""), platform_job_id=str(job.get("platform_job_id") or ""),
        district=str(job.get("district") or ""), salary=str(job.get("salary") or ""),
        experience=str(job.get("experience") or ""), education=str(job.get("education") or ""),
        employment_type=str(job.get("employment_type") or ""),
    )


def _counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    presence = Counter(str(job.get("presence") or "") for job in jobs)
    return {
        "unique_jobs": len(jobs),
        "main_only_jobs": presence["main_only"],
        "supplement_only_jobs": presence["supplement_only"],
        "overlap_jobs": presence["main_and_supplement"],
    }


def _remove_anchor_annotations(job: dict[str, Any]) -> None:
    for key in (
        "known_in_anchor_history", "anchor_history_status", "anchor_first_seen_at",
        "anchor_first_run_id", "anchor_history_scope",
    ):
        job.pop(key, None)
