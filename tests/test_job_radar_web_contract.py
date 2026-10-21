from pathlib import Path


WEB = Path(__file__).parents[1] / "src" / "job_radar" / "web"


def test_web_uses_documented_api_routes_and_csrf_header():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    for route in ("/api/bootstrap", "/api/templates", "/api/runs", "/stop", "/resume", "/export", "/api/jobs/", "/api/onboarding/status", "/api/onboarding/open-extension", "/api/onboarding/open-boss", "/api/app/quit"):
        assert route in script
    assert "X-Job-Radar-Token" in script
    assert "response.json()).error" in script


def test_web_is_csp_safe_and_explains_demo_and_budget_scope():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "Job Radar｜BOSS 直聘职位搜索与求职助手" in html
    assert "BOSS 直聘职位搜索，整理你的求职清单。" in html
    assert "采集调用预算" in html
    assert "不代表浏览器或系统的全部 HTTP 请求" in html
    assert "onclick=" not in html
    assert "<script>" not in html
    assert "style=" not in html


def test_web_handles_queue_history_and_truthful_detail_labels():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    for item in ("queued: '等待开始'", "runHistory", "export_pending", "去重岗位", "待补 JD", "已筛除", "未选择采集 JD"):
        assert item in script
    assert "Chrome 扩展已连接" in script
    assert "先体验示例" in script
    assert "extension_path" in script


def test_web_onboarding_explains_manual_extension_connection():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for item in ("先体验示例", "连接 BOSS", "chrome://extensions", "加载已解压的扩展程序", "复制路径", "刷新连接"):
        assert item in html
    assert "自动修改扩展权限" in html
