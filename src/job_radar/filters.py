"""Explainable local filters. Missing facts are never silently treated as facts."""

from __future__ import annotations

import re
from typing import Any

from boss_agent_cli.search_filters import WELFARE_KEYWORDS


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(x) for x in value)
    return str(value or "")


def salary_range(text: str) -> tuple[float, float] | None:
    text = text.replace(" ", "").upper()
    if not text or "面议" in text or any(x in text for x in ("/天", "/时", "日结", "时薪", "/年")):
        return None
    below = re.search(r"(\d+(?:\.\d+)?)K以下", text)
    above = re.search(r"(\d+(?:\.\d+)?)K以上", text)
    if below:
        return 0, float(below[1])
    if above:
        return float(above[1]), float("inf")
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-~—–]\s*(\d+(?:\.\d+)?)\s*(K|万|元)", text)
    if match:
        factor = {"K": 1, "万": 10, "元": .001}[match[3]]
        return float(match[1]) * factor, float(match[2]) * factor
    match = re.search(r"(\d+(?:\.\d+)?)\s*(K|万|元/月)", text)
    if match:
        amount = float(match[1]) * {"K": 1, "万": 10, "元/月": .001}[match[2]]
        return amount, amount
    return None


def evaluate_job(job: dict, filters: dict, *, detail_pending: bool = False) -> tuple[bool, str]:
    missing: list[str] = []
    text = " ".join(_text(job.get(k)) for k in
                    ("title", "company", "skills", "welfare", "description", "list_summary", "job_type")).casefold()
    title_type = " ".join(_text(job.get(k)) for k in ("title", "job_type"))
    if filters.get("exclude_internships") and "实习" in title_type:
        return False, "排除实习岗位"
    if filters.get("exclude_hourly_daily") and re.search(r"日结|小时工|时薪|/天|/小时|元/时", title_type + _text(job.get("salary"))):
        return False, "排除日结或小时工"
    excluded = [x for x in filters.get("exclude", []) if x.casefold() in text]
    if excluded:
        return False, "包含排除词：" + "、".join(excluded)
    labels = {"salary": "薪资", "experience": "经验", "education": "学历", "industry": "行业",
              "scale": "公司规模", "stage": "融资阶段", "job_type": "职位类型"}
    for field, label in labels.items():
        selected = [x.strip() for x in filters.get(field, "").split(",") if x.strip() and x.strip() != "不限"]
        if not selected:
            continue
        actual = _text(job.get(field))
        if not actual or actual in {"未知", "未提供", "面议"}:
            missing.append(label)
            continue
        if field == "salary":
            got = salary_range(actual)
            ranges = [salary_range(x) for x in selected]
            if got is None or any(x is None for x in ranges):
                missing.append(label)
            elif not any(got[0] <= choice[1] and got[1] >= choice[0] for choice in ranges if choice):
                return False, "薪资区间不重叠"
        elif actual not in selected and actual not in {"不限", "经验不限", "学历不限"}:
            return False, f"{label}不在选择范围内（{actual}）"
    full_detail = job.get("detail_status") in {"complete", "fetched"} and bool(job.get("description"))
    included = filters.get("include", [])
    if included and not any(x.casefold() in text for x in included):
        if not full_detail:
            missing.append("内容关键词（需完整 JD）")
        else:
            return False, "未命中任何内容关键词"
    for welfare in filters.get("welfare", []):
        synonyms = WELFARE_KEYWORDS.get(welfare, [welfare])
        if not any(x.casefold() in text for x in synonyms):
            # A JD omitting a benefit is not evidence the benefit is absent.
            missing.append("福利：" + welfare)
    if missing:
        if filters.get("unknown", "keep") == "exclude" and not detail_pending:
            return False, "信息不足，按设置排除：" + "、".join(missing)
        return True, "待核实：" + "、".join(missing)
    return True, "符合已设置的筛选条件" if any(filters.values()) else "未设置额外筛选条件"
