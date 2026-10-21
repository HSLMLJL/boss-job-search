import asyncio
import hashlib
import json
import socket
import threading

from aiohttp.test_utils import TestClient, TestServer
import pytest

from job_radar.config import TaskConfig, DEFAULT_CONFIG
from job_radar.desktop import InstanceLock, bind_local, existing_address
from job_radar.onboarding import extension_data, bundled_bridge_extension_path
from job_radar.server import create_app
from job_radar.service import Workspace


def test_instance_lock_released_and_port_collision(tmp_path):
    first = InstanceLock(tmp_path / "lock")
    second = InstanceLock(tmp_path / "lock")
    assert first.acquire()
    assert not second.acquire()
    first.close()
    assert second.acquire()
    second.close()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with bind_local(occupied.getsockname()[1]) as actual:
            assert actual.getsockname()[0] == "127.0.0.1"
            assert actual.getsockname()[1] != occupied.getsockname()[1]


def test_existing_instance_does_not_trust_url_or_bad_port(tmp_path):
    path = tmp_path / "instance.json"
    for port in [True, 80, 65536, "8766"]:
        path.write_text(json.dumps({"port": port, "instance_id": "a" * 32,
                                   "url": "https://example.com"}), encoding="utf-8")
        assert existing_address(path) is None


def test_extension_verification_requires_code_and_identity(monkeypatch):
    path = bundled_bridge_extension_path()
    version = json.loads((path / "manifest.json").read_text(encoding="utf-8"))["version"]
    status = {"extensionConnected": True, "extensionId": "a" * 32,
              "extensionVersion": version,
              "extensionBuildHash": hashlib.sha256((path / "background.js").read_bytes()).hexdigest()}
    monkeypatch.setattr("boss_agent_cli.bridge.client.BridgeClient.status", lambda _: status)
    assert extension_data()["verified"]
    status["extensionBuildHash"] = "wrong"
    assert not extension_data()["verified"]


def test_onboarding_demo_and_quit_security(tmp_path, monkeypatch):
    monkeypatch.setattr("job_radar.server.auth_status", lambda _: {"ready": False, "message": "请连接 Chrome"})
    opened = []
    monkeypatch.setattr("job_radar.server.open_extension_folder", lambda: opened.append("extension"))
    monkeypatch.setattr("job_radar.server.open_boss", lambda: opened.append("boss"))
    quit_requested = []

    async def check():
        app = create_app(tmp_path, packaged=True, instance_id="a" * 32, request_quit=lambda: quit_requested.append(True))
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            boot = await (await client.get("/api/bootstrap")).json()
            assert boot["onboarding"]["packaged"]
            assert boot["onboarding"]["extension_path"]
            status = await (await client.get("/api/onboarding/status")).json()
            assert status["ready"] is False
            headers = {"X-Job-Radar-Token": boot["csrf_token"]}
            for route in ["open-extension", "open-boss"]:
                assert (await client.post(f"/api/onboarding/{route}", json={})).status == 403
                assert (await client.post(f"/api/onboarding/{route}", json={}, headers=headers)).status == 200
            assert opened == ["extension", "boss"]
            assert (await client.post("/api/runs", json={"config": DEFAULT_CONFIG}, headers=headers)).status == 409
            response = await client.post("/api/runs", json={"config": DEFAULT_CONFIG, "demo": True}, headers=headers)
            assert response.status == 201
            assert (await client.post("/api/app/quit", json={})).status == 403
            assert (await client.post("/api/app/quit", json={}, headers=headers)).status == 200
            assert (await client.post("/api/runs", json={"config": DEFAULT_CONFIG, "demo": True}, headers=headers)).status == 400
            await asyncio.sleep(0.35)
            assert quit_requested == [True]
    asyncio.run(check())


def test_shutdown_waits_for_saving_and_blocks_new_runs(tmp_path):
    workspace = Workspace(tmp_path)
    stopped, saved = threading.Event(), threading.Event()

    def finish():
        stopped.wait(5)
        saved.set()

    thread = threading.Thread(target=finish)
    workspace.active["fake"] = (thread, stopped)
    thread.start()
    workspace.shutdown()
    workspace.wait_for_shutdown()
    assert saved.is_set() and not thread.is_alive()
    with pytest.raises(ValueError, match="退出"):
        workspace.start(TaskConfig.from_dict(DEFAULT_CONFIG), demo=True)


def test_shutdown_deadline_does_not_hang_on_unresponsive_worker(tmp_path):
    workspace = Workspace(tmp_path)
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    workspace.active["slow"] = (thread, threading.Event())
    thread.start()
    try:
        workspace.shutdown()
        assert workspace.wait_for_shutdown(timeout=0.01) is False
    finally:
        release.set()
        thread.join()


def test_missing_extension_keeps_demo_bootstrap_available(tmp_path, monkeypatch):
    def missing():
        raise FileNotFoundError("missing")
    monkeypatch.setattr("job_radar.server.install_extension_path", missing)
    monkeypatch.setattr("job_radar.onboarding.extension_data", missing)

    async def check():
        async with TestClient(TestServer(create_app(tmp_path))) as client:
            response = await client.get("/api/bootstrap")
            assert response.status == 200
            boot = await response.json()
            assert boot["onboarding"]["extension_path"] == ""
            assert "重新完整解压" in boot["auth"]["message"]
    asyncio.run(check())
