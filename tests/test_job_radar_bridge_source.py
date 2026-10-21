from __future__ import annotations
import pytest
from job_radar.bridge_source import BridgeJobRadarSource
from job_radar.source import SourceAuthRequired, SourceError, SourceRiskStopped


class Result:
	def __init__(self, ok=True, data=None, error=""):
		self.ok, self.data, self.error = ok, data, error


class Bridge:
	def __init__(self, data=None, error=None):
		self.data = data or {
			"code": 0,
			"zpData": {
				"jobList": [{"encryptJobId": "job1", "jobName": "产品经理", "brandName": "示例", "cityName": "上海"}],
				"hasMore": False,
				"pageState": {
					"currentPage": 1,
					"identityMatched": True,
					"terminal": True,
					"pageSize": 1,
					"hasMore": False,
				},
			},
		}
		self.error = error
		self.calls = []

	def capture_job_list(self, url, **kwargs):
		self.calls.append((url, kwargs))
		if self.error:
			raise RuntimeError(self.error)
		return self.data

	def radar_tab_status(self):
		return {"prepared": True, "tabId": 8, "url": self.calls[-1][0]}

	def send_command(self, action, **kwargs):
		self.calls.append((action, kwargs))
		url = self.calls[0][0]
		mode = kwargs["panel_mode"]
		if mode == "inspect":
			return Result(data={"status": "ready", "href": url, "cards": [{"stable_job_id": "job1"}]})
		if mode == "select":
			return Result(data={"status": "selected", "href": url, "selected_job_id": "job1"})
		return Result(
			data={
				"status": "ready",
				"href": url,
				"stable_job_id": "job1",
				"jd_text": "完整JD",
				"identity_evidence": {
					"stable_job_id": "job1",
					"exact_url": url,
					"detail_url": "https://www.zhipin.com/job_detail/job1.html",
				},
			}
		)


def source(bridge):
	return BridgeJobRadarSource(bridge=bridge, extension_status_reader=lambda: {"verified": True})


def test_bridge_search_success_and_keeps_filters_local():
	bridge = Bridge()
	item = source(bridge)
	item.open()
	result = item.search("产品经理", "上海", 1, {"education": "本科"})
	assert result["has_more"] is False and result["jobs"][0]["description"] == ""
	assert "education" not in bridge.calls[0][0]


def test_bridge_code_risk_and_identity_mismatch_fail_closed():
	item = source(Bridge({"code": 37, "zpData": {}}))
	item.open()
	with pytest.raises(SourceRiskStopped):
		item.search("产品经理", "上海", 1, {})
	bad = Bridge(
		{
			"code": 0,
			"zpData": {
				"jobList": [],
				"hasMore": False,
				"pageState": {
					"currentPage": 1,
					"identityMatched": False,
					"terminal": True,
					"pageSize": 0,
					"hasMore": False,
				},
			},
		}
	)
	item = source(bad)
	item.open()
	with pytest.raises(SourceError):
		item.search("产品经理", "上海", 1, {})


def test_bridge_login_error_and_detail_empty_are_safe():
	item = source(Bridge(error="login required"))
	item.open()
	with pytest.raises(SourceAuthRequired):
		item.search("产品经理", "上海", 1, {})
	bridge = Bridge()
	item = source(bridge)
	item.open()
	job = item.search("产品经理", "上海", 1, {})["jobs"][0]

	def empty_panel(action, **kwargs):
		url = bridge.calls[0][0]
		if kwargs["panel_mode"] == "inspect":
			return Result(data={"status": "ready", "href": url, "cards": [{"stable_job_id": "job1"}]})
		if kwargs["panel_mode"] == "select":
			return Result(data={"status": "selected", "href": url, "selected_job_id": "job1"})
		return Result(
			data={
				"status": "ready",
				"href": url,
				"stable_job_id": "job1",
				"jd_text": "",
				"identity_evidence": {
					"stable_job_id": "job1",
					"exact_url": url,
					"detail_url": "https://www.zhipin.com/job_detail/job1.html",
				},
			}
		)

	bridge.send_command = empty_panel
	assert item.detail(job)["detail_status"] == "empty"


def test_bridge_detail_commands_are_never_replayed():
	bridge = Bridge()
	item = source(bridge)
	item.open()
	job = item.search("产品经理", "上海", 1, {})["jobs"][0]
	assert item.detail(job)["detail_status"] == "fetched"
	for call in bridge.calls[1:]:
		if call[0] == "read-search-job-panel":
			assert call[1]["allow_replay"] is False
