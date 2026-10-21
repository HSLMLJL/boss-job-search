"""One validated configuration shared by the web interface, CLI and runner."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from boss_agent_cli.api import endpoints
from boss_agent_cli.search_filters import WELFARE_KEYWORDS

FIELD_LABELS = {
    "title": "岗位名称", "company": "公司", "city": "城市", "district": "区域",
    "salary": "薪资", "experience": "经验要求", "education": "学历要求",
    "industry": "行业", "scale": "公司规模", "stage": "融资阶段", "job_type": "职位类型",
    "welfare": "福利", "skills": "技能标签", "description": "完整 JD",
    "address": "工作地址", "url": "岗位链接", "detail_status": "JD 采集状态",
    "matched": "筛选结果", "filter_reason": "筛选依据", "queries": "命中关键词",
    "first_seen": "首次收录时间", "last_seen": "最近收录时间", "published_at": "平台发布时间",
    "change": "收录变化", "status": "我的进度", "notes": "我的备注",
}

FILTER_LOOKUPS = {
    "salary": endpoints.SALARY_CODES, "experience": endpoints.EXPERIENCE_CODES,
    "education": endpoints.EDUCATION_CODES, "industry": endpoints.INDUSTRY_CODES,
    "scale": endpoints.SCALE_CODES, "stage": endpoints.STAGE_CODES, "job_type": endpoints.JOB_TYPE_CODES,
}
DEFAULT_CONFIG = {
    "name": "我的求职计划", "keywords": ["产品经理"], "cities": ["上海"], "details": "matched",
    "filters": {
        "salary": "", "experience": "", "education": "", "industry": "", "scale": "", "stage": "",
        "job_type": "", "welfare": [], "include": [], "exclude": [],
        "exclude_internships": False, "exclude_hourly_daily": False, "unknown": "keep",
    },
    "limits": {"pages": 3, "requests": 30, "details": 20, "jobs": 200, "seconds": 600},
    "export": {
        "formats": ["xlsx"], "fields": ["title", "company", "city", "salary", "experience", "education",
        "description", "url", "detail_status", "filter_reason", "first_seen", "last_seen", "change", "status"],
        "scope": "matched",
    },
}
LIMIT_RANGES = {"pages": (1, 100), "requests": (1, 2000), "details": (0, 1000), "jobs": (1, 5000), "seconds": (10, 14400)}


def _list(value: Any, name: str, *, maximum: int = 50, item_length: int = 100) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{name} 必须是文本列表")
    result = list(dict.fromkeys(x.strip() for x in value if x.strip()))
    if len(result) > maximum or any(len(x) > item_length for x in result):
        raise ValueError(f"{name} 最多 {maximum} 项，每项最多 {item_length} 字")
    if any(any(ord(c) < 32 for c in x) for x in result):
        raise ValueError(f"{name} 不能包含控制字符")
    return result


@dataclass(frozen=True)
class TaskConfig:
    name: str
    keywords: list[str]
    cities: list[str]
    details: str
    filters: dict[str, Any]
    limits: dict[str, int]
    export: dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskConfig":
        if not isinstance(value, dict):
            raise ValueError("任务配置必须是对象")
        unknown = set(value) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError("无法识别的配置项：" + ", ".join(sorted(unknown)))
        data = deepcopy(DEFAULT_CONFIG)
        for key, item in value.items():
            if key in {"filters", "limits", "export"}:
                if not isinstance(item, dict):
                    raise ValueError(f"{key} 必须是对象")
                extra = set(item) - set(data[key])
                if extra:
                    raise ValueError(f"{key} 中有无法识别的配置项：{', '.join(sorted(extra))}")
                data[key].update(item)
            else:
                data[key] = item
        if not isinstance(data["name"], str) or not data["name"].strip() or len(data["name"]) > 100:
            raise ValueError("计划名称需要 1–100 字")
        data["name"] = data["name"].strip()
        data["keywords"] = _list(data["keywords"], "搜索关键词", maximum=30)
        data["cities"] = _list(data["cities"], "城市", maximum=50)
        if not data["keywords"] or not data["cities"]:
            raise ValueError("请至少填写一个关键词和一个城市")
        for city in data["cities"]:
            if city not in endpoints.CITY_CODES:
                raise ValueError(f"不支持的城市：{city}；请从城市列表中选择")
        if data["details"] not in ("none", "matched", "all"):
            raise ValueError("JD 采集方式应为 none、matched 或 all")
        filters = data["filters"]
        for key, lookup in FILTER_LOOKUPS.items():
            if not isinstance(filters[key], str):
                raise ValueError(f"{key} 筛选值需要文本")
            parts = _list(filters[key].replace("，", ",").split(","), key)
            for item in parts:
                if item not in lookup:
                    raise ValueError(f"不支持的 {key} 筛选值：{item}")
            filters[key] = ",".join(parts)
        for key in ("include", "exclude", "welfare"):
            filters[key] = _list(filters[key], key)
        for key in ("exclude_internships", "exclude_hourly_daily"):
            if not isinstance(filters[key], bool):
                raise ValueError(f"{key} 必须为开关值")
        if filters["unknown"] not in ("keep", "exclude"):
            raise ValueError("信息缺失处理方式应为 keep 或 exclude")
        for key, (low, high) in LIMIT_RANGES.items():
            n = data["limits"][key]
            if isinstance(n, bool) or not isinstance(n, int) or not low <= n <= high:
                raise ValueError(f"{key} 需要是 {low}–{high} 之间的整数")
        if data["details"] != "none" and data["limits"]["details"] == 0:
            raise ValueError("启用 JD 采集时，详情数量上限必须大于 0")
        output = data["export"]
        output["formats"] = _list(output["formats"], "导出格式", maximum=3)
        output["fields"] = _list(output["fields"], "导出字段", maximum=len(FIELD_LABELS))
        if not output["formats"] or set(output["formats"]) - {"xlsx", "csv", "json"}:
            raise ValueError("请选择 Excel、CSV、JSON 中至少一种导出格式")
        if not output["fields"] or set(output["fields"]) - set(FIELD_LABELS):
            raise ValueError("请选择有效的导出字段")
        if output["scope"] not in ("matched", "all", "new"):
            raise ValueError("导出范围应为 matched、all 或 new")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self.__dict__)


def options() -> dict[str, Any]:
    return {"cities": list(endpoints.CITY_CODES), **{key: list(v) for key, v in FILTER_LOOKUPS.items()},
            "welfare": list(WELFARE_KEYWORDS), "fields": FIELD_LABELS}
