from __future__ import annotations

from pathlib import Path
from typing import Any

from job_radar.config import TaskConfig
from job_radar.runner import run_task
from job_radar.store import JobStore


class FixtureSource:
	is_demo = True

	def __init__(self, jobs: list[dict[str, Any]]) -> None:
		self.jobs = jobs
		self.open_count = 0
		self.close_count = 0
		self.details: list[str] = []

	def open(self) -> None:
		self.open_count += 1

	def close(self) -> None:
		self.close_count += 1

	def search(self, query: str, city: str, page: int, filters: dict[str, Any]) -> dict[str, Any]:
		del query, city, filters
		return {"jobs": self.jobs if page == 1 else [], "has_more": False}

	def detail(self, job: dict[str, Any]) -> dict[str, Any]:
		self.details.append(job["job_key"])
		return {
			"description": f"完整 JD {job['job_key']}：企业产品工作流",
			"skills": ["产品设计"],
			"welfare": ["五险一金"],
			"detail_status": "fetched",
		}


def _job(key: str) -> dict[str, Any]:
	return {
		"job_key": key,
		"title": "产品经理",
		"company": "测试公司",
		"city": "上海",
		"district": "浦东",
		"salary": "20-30K",
		"experience": "3-5年",
		"education": "本科",
		"industry": "企业服务",
		"scale": "100-499人",
		"stage": "B轮",
		"job_type": "全职",
		"welfare": [],
		"skills": [],
		"description": "列表里的短摘要不能当 JD",
		"list_summary": "列表里的短摘要不能当 JD",
		"url": "https://example.invalid/job",
		"detail_status": "not_requested",
		"_security_id": f"sid-{key}",
	}


def _config(*, details: str, detail_limit: int = 2, include: list[str] | None = None) -> TaskConfig:
	return TaskConfig.from_dict(
		{
			"name": "测试计划",
			"keywords": ["产品经理"],
			"cities": ["上海"],
			"details": details,
			"filters": {"include": include or []},
			"limits": {"pages": 1, "requests": 10, "details": detail_limit, "jobs": 20, "seconds": 60},
		}
	)


def test_none_never_persists_list_summary_as_full_jd(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_none_001")])
	result = run_task(_config(details="none"), store, source)
	row = store.get_jobs(result["id"])[0]
	assert result["status"] == "completed"
	assert source.details == []
	assert row["description"] == ""
	assert row["list_summary"] == "列表里的短摘要不能当 JD"
	assert row["detail_status"] == "not_requested"


def test_matched_collects_detail_then_filters_again(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_matched_001")])
	result = run_task(_config(details="matched", include=["工作流"]), store, source)
	row = store.get_jobs(result["id"])[0]
	assert result["status"] == "completed"
	assert source.details == ["demo_matched_001"]
	assert row["detail_status"] == "fetched"
	assert row["matched"] is True
	assert row["description"].startswith("完整 JD")


def test_explicit_resume_gets_a_new_budget_window(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_resume_001"), _job("demo_resume_002")])
	config = _config(details="all", detail_limit=1)
	first = run_task(config, store, source)
	assert first["status"] == "budget_stopped"
	assert len(source.details) == 1
	second = run_task(config, store, source, run_id=first["id"])
	rows = store.get_jobs(first["id"])
	assert second["status"] == "completed"
	assert len(source.details) == 2
	assert {row["detail_status"] for row in rows} == {"fetched"}


def test_duplicate_across_keywords_keeps_one_fetched_jd(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_shared_001")])
	config = TaskConfig.from_dict(
		{
			"name": "去重",
			"keywords": ["产品经理", "企业产品"],
			"cities": ["上海"],
			"details": "all",
			"limits": {"pages": 1, "requests": 10, "details": 3, "jobs": 20, "seconds": 60},
		}
	)
	result = run_task(config, store, source)
	rows = store.get_jobs(result["id"])
	assert result["status"] == "completed"
	assert source.details == ["demo_shared_001"]
	assert rows[0]["queries"] == ["产品经理", "企业产品"]
	assert rows[0]["detail_status"] == "fetched"


def test_jobs_hard_cap_preserves_unprocessed_rows_for_resume(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_cap_001"), _job("demo_cap_002")])
	raw = _config(details="none").to_dict()
	raw["limits"]["jobs"] = 1
	config = TaskConfig.from_dict(raw)
	first = run_task(config, store, source)
	assert first["status"] == "budget_stopped"
	assert len(store.get_jobs(first["id"])) == 1
	assert len(first["checkpoint"]["pending_rows"]) == 1
	second = run_task(config, store, source, run_id=first["id"])
	assert second["status"] == "completed"
	assert len(store.get_jobs(first["id"])) == 2


def test_page_window_is_partial_and_matched_count_is_persisted(tmp_path: Path) -> None:
	class MoreSource(FixtureSource):
		def search(self, query: str, city: str, page: int, filters: dict[str, Any]) -> dict[str, Any]:
			del query, city, filters
			return {"jobs": self.jobs if page == 1 else [], "has_more": page == 1}

	store = JobStore(tmp_path)
	result = run_task(_config(details="none"), store, MoreSource([_job("demo_partial_001")]))
	assert result["status"] == "partial"
	assert result["counts"]["matched"] == 1
	pair = result["checkpoint"]["pairs"]["产品经理␟上海"]
	assert pair["partial"] is True and pair["next_page"] == 2


def test_empty_detail_is_requested_once_and_not_counted_as_fetched(tmp_path: Path) -> None:
	class EmptyDetail(FixtureSource):
		def detail(self, job: dict[str, Any]) -> dict[str, Any]:
			self.details.append(job["job_key"])
			return {"detail_status": "empty"}

	store = JobStore(tmp_path)
	result = run_task(_config(details="all"), store, EmptyDetail([_job("demo_empty_001")]))
	row = store.get_jobs(result["id"])[0]
	assert result["counts"]["details_requested"] == 1
	assert result["counts"]["details_fetched"] == 0
	assert result["counts"]["details_empty"] == 1
	assert row["detail_status"] == "empty"


def test_matched_keeps_partial_row_unmatched_when_full_jd_is_required(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_pending_001")])
	raw = _config(details="matched", detail_limit=1, include=["工作流"]).to_dict()
	raw["limits"]["requests"] = 1
	raw["filters"]["unknown"] = "exclude"
	config = TaskConfig.from_dict(raw)
	result = run_task(config, store, source)
	row = store.get_jobs(result["id"])[0]
	assert result["status"] == "budget_stopped"
	assert row["detail_status"] == "pending"
	assert row["matched"] is False


def test_cached_rows_can_be_consumed_when_request_window_is_exhausted(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_cached_001")])
	raw = _config(details="none").to_dict()
	raw["limits"]["requests"] = 1
	raw["limits"]["jobs"] = 1
	config = TaskConfig.from_dict(raw)
	result = run_task(config, store, source)
	assert result["status"] == "completed"
	assert len(store.get_jobs(result["id"])) == 1


def test_job_cap_still_completes_budgeted_details_for_saved_rows(tmp_path: Path) -> None:
	store = JobStore(tmp_path)
	source = FixtureSource([_job("demo_cap_jd_001"), _job("demo_cap_jd_002")])
	raw = _config(details="all", detail_limit=1).to_dict()
	raw["limits"].update(jobs=1, requests=2)
	result = run_task(TaskConfig.from_dict(raw), store, source)
	assert result["status"] == "budget_stopped"
	assert result["counts"]["details_fetched"] == 1
	assert len(store.get_jobs(result["id"])) == 1
	assert len(result["checkpoint"]["pending_rows"]) == 1
	assert source.details == ["demo_cap_jd_001"]
