"""Clearly marked synthetic data source for local demos and tests."""

from __future__ import annotations
from copy import deepcopy
import hashlib
from typing import Any


class DemoSource:
	"""Offline fixture. Rows are fabricated and have no platform URL."""

	is_demo = True

	def __init__(self) -> None:
		self.opened = False
		self.calls = []
		self.detail_calls = []

	def open(self) -> None:
		self.opened = True

	def search(self, query: str, city: str, page: int, filters: dict[str, Any]) -> dict[str, Any]:
		del filters
		if not self.opened:
			raise RuntimeError("demo source is not open")
		self.calls.append((query, city, page))
		if page != 1:
			return {"jobs": [], "has_more": False}
		city = city or "上海"
		key_city = hashlib.sha256(city.encode("utf-8")).hexdigest()[:8]
		rows = []
		for n, (title, summary, experience) in enumerate(
			(
				(query or "产品经理", "负责需求调研、方案设计与跨团队交付。", "3-5年"),
				(f"{query or '产品'}策略专员", "支持市场调研和产品数据分析。", "1-3年"),
				(f"{query or '产品'}实习生", "协助资料整理与用户访谈。", "在校/应届"),
			),
			1,
		):
			rows.append(
				{
					"job_key": f"demo_{key_city}_{n:02d}",
					"title": title,
					"company": f"{city}示例企业{n}",
					"city": city,
					"district": "示例区",
					"address": "合成园区",
					"salary": "15-25K" if n < 3 else "5-8K",
					"experience": experience,
					"education": "本科",
					"industry": "企业服务",
					"scale": "100-499人",
					"stage": "B轮",
					"job_type": "全职",
					"welfare": ["五险一金"] if n != 3 else [],
					"skills": ["需求分析", "沟通协作"] if n == 1 else ["数据分析"],
					"description": "",
					"list_summary": summary,
					"url": "",
					"detail_status": "not_requested",
					"_security_id": f"demo-security-{key_city}-{n}",
				}
			)
		return {"jobs": deepcopy(rows), "has_more": False}

	def detail(self, job: dict[str, Any]) -> dict[str, Any]:
		key = str(job.get("job_key") or "")
		self.detail_calls.append(key)
		if key.endswith("_03"):
			return {"detail_status": "empty"}
		return {
			"description": "合成完整 JD：负责需求调研、流程设计、上线验收与持续迭代；该文本仅供离线演示。",
			"skills": ["需求分析", "沟通协作", "上线验收"],
			"welfare": ["五险一金"],
			"detail_status": "fetched",
		}

	def close(self) -> None:
		self.opened = False


def demo_source() -> DemoSource:
	return DemoSource()
