"""Build a strict, renderer-neutral workbook view from a delivery bundle.

This module only validates and reshapes already-selected delivery data.  It
does not read or write SQLite, scan pages, or create an XLSX file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import quote, urlsplit
import re

from boss_agent_cli.radar.history_anchor import (
    HistoryAnchorError,
    validate_history_anchor_annotations,
)


_CHINA_TZ = timezone(timedelta(hours=8))
_SAFE_ID = re.compile(r"^[A-Za-z0-9_~-]+$")
_STATUS_LABELS = {
    "known_old": "已知岗位",
    "first_observed_today": "当日首次观察",
    "absent_from_anchor_at_cutoff": "锚点截至截止时间未见",
    "no_stable_platform_id": "缺少稳定岗位ID",
    "ambiguous_anchor_platform_id": "锚点ID歧义",
    "anchor_history_not_ai_pm_relevant": "锚点历史非相关",
    "anchor_first_seen_invalid": "锚点首次观察时间无效",
}


class DeliveryWorkbookViewError(ValueError):
    """The supplied delivery bundle cannot safely be rendered."""


def build_delivery_workbook_view(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return plain JSON-ready rows for ``本次扫描`` and ``扫描摘要``.

    A history-enriched bundle is required so the renderer cannot silently drop
    the anchor classification that distinguishes prior local observations from
    an anchor miss.  Neither label implies platform publication time.
    """
    if not isinstance(bundle, Mapping):
        raise DeliveryWorkbookViewError("delivery bundle must be an object")
    try:
        validate_history_anchor_annotations(bundle)
    except HistoryAnchorError as exc:
        raise DeliveryWorkbookViewError(f"history anchor validation failed: {exc}") from exc

    jobs = bundle.get("jobs")
    counts = bundle.get("counts")
    coverage = bundle.get("coverage")
    window = bundle.get("scan_window")
    if not isinstance(jobs, list) or not isinstance(counts, Mapping):
        raise DeliveryWorkbookViewError("bundle jobs and counts are required")
    if not isinstance(coverage, Mapping) or not isinstance(window, Mapping):
        raise DeliveryWorkbookViewError("bundle coverage and scan_window are required")
    if counts.get("unique_jobs") != len(jobs):
        raise DeliveryWorkbookViewError("counts.unique_jobs must equal jobs length")

    rows = sorted((_job_row(job) for job in jobs), key=_row_sort_key)
    identities = {(row["平台"], row["稳定岗位ID"]) for row in rows}
    if len(identities) != len(rows):
        raise DeliveryWorkbookViewError("duplicate platform + stable job ID in delivery bundle")

    main_sources = _sources(coverage, "main_sources")
    supplement_sources = _sources(coverage, "supplement_sources")
    source_rows = [_source_row(item) for item in (*main_sources, *supplement_sources)]
    if not source_rows:
        raise DeliveryWorkbookViewError("at least one explicit source is required")
    summary_rows = [
        {"项目": "交付范围", "内容": "仅合并明确来源的已验证扫描链；不代表全国或全城岗位穷尽。"},
        {"项目": "采集窗口", "内容": f"{_china_time(window.get('start_at'))} 至 {_china_time(window.get('end_at'))}"},
        {"项目": "唯一岗位数", "内容": len(rows)},
        {"项目": "锚点说明", "内容": str(bundle["history_anchor"].get("cutoff_semantics"))},
        {"项目": "锚点未命中说明", "内容": str(bundle["history_anchor"].get("absence_semantics"))},
    ]
    exclusions = bundle.get("exclusion_summary")
    if isinstance(exclusions, Mapping):
        summary_rows.append({"项目": "实习及日时薪过滤", "内容": (
            f"按标题、薪资和可用岗位类型过滤，移除 {exclusions.get('removed', 0)} 条；"
            f"{exclusions.get('limitation', '未知字段不推断为正式全职。')}"
        )})
    summary_rows.extend(
        {
            "项目": f"来源覆盖：{item['来源']}",
            "内容": (
                f"城市：{item['城市']}；关键词：{item['关键词']}；"
                f"实际时间：{item['开始时间']} 至 {item['完成时间']}；{item['覆盖说明']}"
            ),
        }
        for item in source_rows
    )
    return {
        "schema_version": 1,
        "sheets": {
            "本次扫描": {"columns": list(_JOB_COLUMNS), "rows": rows},
            "扫描摘要": {"columns": ["项目", "内容"], "rows": summary_rows},
        },
        "sources": source_rows,
        "scan_window": {
            "start_at": _china_time(window.get("start_at")),
            "end_at": _china_time(window.get("end_at")),
            "timezone": "Asia/Shanghai",
        },
        "limitations": "岗位来自明确扫描来源；页面终点和锚点状态均不代表平台全量或发布日期。",
    }


_JOB_COLUMNS = (
    "平台", "稳定岗位ID", "岗位链接", "岗位名称", "公司", "岗位城市", "区域", "薪资", "经验", "学历",
    "AI证据", "最早观察时间", "最新观察时间", "历史状态", "历史证据", "是否repost", "最新位置", "来源溯源",
)


def _job_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DeliveryWorkbookViewError("every job must be an object")
    platform, job_id = _text(value, "platform"), _text(value, "platform_job_id")
    if not platform or not _SAFE_ID.fullmatch(job_id):
        raise DeliveryWorkbookViewError("every delivery job requires an exact stable platform ID")
    url = _text(value, "boss_url")
    expected_url = f"https://www.zhipin.com/job_detail/{quote(job_id, safe='')}.html"
    if url != expected_url or not _canonical_url(url, job_id):
        raise DeliveryWorkbookViewError("job link must be the canonical link for its stable ID")
    source_first = _parse_time(value.get("first_discovered_at"))
    anchor_first = _parse_time(value.get("anchor_first_seen_at")) if value.get("anchor_first_seen_at") is not None else None
    first = _format_china_time(min(item for item in (source_first, anchor_first) if item is not None))
    latest = _china_time(value.get("latest_observed_at"))
    latest_observation = value.get("latest_observation")
    if not isinstance(latest_observation, Mapping):
        raise DeliveryWorkbookViewError("job latest_observation is required")
    for field in ("source", "source_id", "query", "shard", "observed_at"):
        if not _text(latest_observation, field):
            raise DeliveryWorkbookViewError(f"job latest_observation.{field} is required")
    if _china_time(latest_observation.get("observed_at")) != latest:
        raise DeliveryWorkbookViewError("latest observation timestamp does not match job latest timestamp")
    if not isinstance(latest_observation.get("page"), int) or not isinstance(latest_observation.get("rank"), int):
        raise DeliveryWorkbookViewError("job latest observation needs integer page and rank")
    status = _text(value, "anchor_history_status")
    if status not in _STATUS_LABELS:
        raise DeliveryWorkbookViewError("job history status is unsupported")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DeliveryWorkbookViewError("job source provenance is required")
    reposted = any(isinstance(source, Mapping) and source.get("reposted_evidence") is True for source in sources)
    evidence = _text(value, "anchor_history_scope")
    if not evidence:
        raise DeliveryWorkbookViewError("job anchor history evidence is required")
    return {
        "平台": platform, "稳定岗位ID": job_id, "岗位链接": url,
        "岗位名称": _text(value, "title"), "公司": _text(value, "company"),
        "岗位城市": _text(value, "city"), "区域": _text(value, "district"),
        "薪资": _text(value, "salary"), "经验": _text(value, "experience"), "学历": _text(value, "education"),
        "AI证据": "；".join(_string_list(value.get("ai_evidence"))),
        "最早观察时间": first, "最新观察时间": latest,
        "历史状态": _STATUS_LABELS[status], "历史证据": evidence,
        "是否repost": "是" if reposted else "否",
        "最新位置": f"{_text(latest_observation, 'shard')} / {_text(latest_observation, 'query')} / 第{latest_observation['page']}批 / #{latest_observation['rank']}",
        "来源溯源": "；".join(_source_provenance(source) for source in sources),
    }


def _sources(coverage: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    values = coverage.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, Mapping) for value in values):
        raise DeliveryWorkbookViewError(f"coverage.{key} must be a source list")
    return list(values)


def _source_row(source: Mapping[str, Any]) -> dict[str, str]:
    for field in ("label", "logical_scan_id", "coverage_label", "started_at", "completed_at"):
        if not _text(source, field):
            raise DeliveryWorkbookViewError(f"source.{field} is required")
    cities, queries = source.get("cities"), source.get("queries")
    if not _string_list(cities) or not _string_list(queries):
        raise DeliveryWorkbookViewError("source cities and queries are required")
    return {
        "来源": _text(source, "label"), "逻辑扫描": _text(source, "logical_scan_id"),
        "城市": "、".join(_string_list(cities)), "关键词": "、".join(_string_list(queries)),
        "开始时间": _china_time(source.get("started_at")), "完成时间": _china_time(source.get("completed_at")),
        "覆盖说明": _text(source, "coverage_label"),
    }


def _source_provenance(source: Any) -> str:
    if not isinstance(source, Mapping):
        raise DeliveryWorkbookViewError("job source entry must be an object")
    for field in ("source_label", "source_id", "source_database", "latest_observed_at"):
        if not _text(source, field):
            raise DeliveryWorkbookViewError(f"job source.{field} is required")
    positions = source.get("positions")
    if not isinstance(positions, list) or not positions:
        raise DeliveryWorkbookViewError("job source positions are required")
    return f"{_text(source, 'source_label')} ({_text(source, 'source_id')}; {_china_time(source.get('latest_observed_at'))})"


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    return item.strip() if isinstance(item, str) else ""


def _string_list(value: Any) -> list[str]:
    return [item.strip() for item in value] if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value) else []


def _china_time(value: Any) -> str:
    return _format_china_time(_parse_time(value))


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DeliveryWorkbookViewError("timestamps must be timezone-aware ISO values") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeliveryWorkbookViewError("timestamps must be timezone-aware ISO values")
    return parsed


def _format_china_time(value: datetime) -> str:
    return value.astimezone(_CHINA_TZ).isoformat(timespec="seconds")


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str, str]:
    status_order = {"当日首次观察": 0, "锚点截至截止时间未见": 1, "已知岗位": 2}
    timestamp = _parse_time(row["最新观察时间"]).timestamp()
    return (
        status_order.get(str(row["历史状态"]), 3),
        -timestamp,
        str(row["平台"]),
        str(row["稳定岗位ID"]),
    )


def _canonical_url(url: str, job_id: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "www.zhipin.com" and not parsed.query and not parsed.fragment and parsed.path == f"/job_detail/{job_id}.html"
