from __future__ import annotations

from job_radar.demo import demo_source
import pytest
from job_radar.source import _stable_key, normalize_job


def test_normalize_card_keeps_list_text_out_of_description() -> None:
	job = normalize_job(
		{
			"encryptJobId": "abc~123",
			"securityId": "volatile",
			"jobName": "产品经理",
			"brandName": "示例公司",
			"cityName": "上海",
			"postDescription": "这只是列表摘要",
			"skills": ["产品设计"],
		}
	)
	assert job["job_key"].startswith("boss_")
	assert job["description"] == ""
	assert job["list_summary"] == "这只是列表摘要"
	assert job["detail_status"] == "not_requested"
	assert job["_security_id"] == "volatile"
	assert job["url"].endswith("abc~123.html")


def test_demo_source_is_explicitly_synthetic_and_offline() -> None:
	source = demo_source()
	assert source.is_demo is True
	source.open()
	response = source.search("产品经理", "北京", 1, {})
	assert response["has_more"] is False
	assert response["jobs"][0]["job_key"].startswith("demo_")
	detail = source.detail(response["jobs"][0])
	assert "合成完整 JD" in detail["description"]
	source.close()


def test_fallback_key_and_platform_filter_codes_are_stable() -> None:
	from job_radar.source import _stable_key, build_platform_params

	assert _stable_key({"brandName": "示例", "jobName": "产品经理", "cityName": "上海"}).startswith("boss_")
	params = build_platform_params({"education": "本科", "job_type": "全职"})
	assert "degree" in params and "jobType" in params


def test_normalize_prefers_names_and_keeps_only_real_publish_time() -> None:
	job = normalize_job(
		{
			"jobId": "one",
			"industryName": "互联网",
			"industry": "999",
			"jobTypeName": "全职",
			"jobType": "2",
			"jobPublishTime": 1700000000000,
			"createTime": 1,
		}
	)
	assert job["industry"] == "互联网"
	assert job["job_type"] == "全职"
	assert job["published_at"].endswith("Z")


def test_source_payload_errors_are_safe_and_status_is_required(tmp_path) -> None:
	from job_radar.source import BossJobRadarSource, SourceError, SourceRiskStopped

	source = BossJobRadarSource(data_dir=tmp_path, cookies={}, user_agent="", cdp_port=0)
	with pytest.raises(SourceError, match="缺少状态码"):
		source._raise_for_payload({"message": "secret-token"})
	with pytest.raises(SourceRiskStopped, match="code 36"):
		source._raise_for_payload({"code": 36, "message": "secret-token"})
	assert _stable_key({"jobId": "AbC"}) != _stable_key({"jobId": "abc"})


def test_security_check_ignores_qrcode_in_visible_jobs_page() -> None:
	from boss_agent_cli.crawler.transport import DrissionCrawlerSession

	class Body:
		text = "职位列表 qrcode"

	class Page:
		url = "https://www.zhipin.com/web/geek/jobs?city=101020100"

		def ele(self, selector, timeout=0):
			return Body() if selector == "tag:body" else None

	session = object.__new__(DrissionCrawlerSession)
	session._page = Page()
	session._raise_if_security_page()


def test_source_maps_browser_login_and_security_to_different_statuses(tmp_path) -> None:
	from boss_agent_cli.crawler.transport import CrawlAuthRequired, CrawlRiskError
	from job_radar.source import BossJobRadarSource, SourceAuthRequired, SourceRiskStopped

	class Page:
		def get(self, url):
			return None

	class Session:
		_page = Page()

		def __init__(self, error):
			self.error = error

		def _raise_if_security_page(self):
			raise self.error

	source = BossJobRadarSource(data_dir=tmp_path, cookies={}, user_agent="", cdp_port=0)
	source._auth_error = (CrawlAuthRequired,)
	source._risk_error = (CrawlRiskError,)
	source._session = Session(CrawlAuthRequired("redacted"))
	with pytest.raises(SourceAuthRequired):
		source.search("产品经理", "上海", 1, {})
	source._session = Session(CrawlRiskError("redacted"))
	with pytest.raises(SourceRiskStopped):
		source.search("产品经理", "上海", 1, {})


@pytest.mark.parametrize("url,text,error", [
	("https://www.zhipin.com/web/user/login", "", "auth"),
	("https://www.zhipin.com/web/geek/jobs", "扫码登录 手机号登录", "auth"),
	("https://www.zhipin.com/web/geek/jobs", "请完成安全验证", "risk"),
	("https://www.zhipin.com/web/common/zhipin-security.html", "", "risk"),
])
def test_visible_login_and_real_security_still_stop(url, text, error) -> None:
	from types import SimpleNamespace
	from boss_agent_cli.crawler.transport import CrawlAuthRequired, CrawlRiskError, DrissionCrawlerSession
	session = object.__new__(DrissionCrawlerSession)
	session._page = SimpleNamespace(url=url, ele=lambda *args, **kwargs: SimpleNamespace(text=text))
	with pytest.raises(CrawlAuthRequired if error == "auth" else CrawlRiskError):
		session._raise_if_security_page()


def test_instance_risk_response_and_safe_source_code(tmp_path) -> None:
	from types import SimpleNamespace
	from boss_agent_cli.crawler.transport import CrawlRiskError, DrissionCrawlerSession
	from job_radar.source import BossJobRadarSource, SourceRiskStopped
	session = object.__new__(DrissionCrawlerSession)
	session._raise_if_risk_response({"code": 0})
	with pytest.raises(CrawlRiskError):
		session._raise_if_risk_response({"code": 37})
	source = BossJobRadarSource(data_dir=tmp_path, cookies={}, user_agent="", cdp_port=0)
	source._session = SimpleNamespace(
		_page=SimpleNamespace(get=lambda url: None), _raise_if_security_page=lambda: None,
	)
	source._xhr = lambda *args: {"code": 37, "message": "secret-token"}
	with pytest.raises(SourceRiskStopped, match="code 37") as exc:
		source.search("产品经理", "上海", 1, {})
	assert "secret-token" not in str(exc.value)
