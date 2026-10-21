"""Portable Windows entry: one local instance, no console or installed Python."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import sys
import time
from urllib.request import ProxyHandler, build_opener
import webbrowser


def default_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Job-Radar"
    return Path.home() / ".job-radar"


class InstanceLock:
    """OS-owned lock, automatically released on exit or crash."""

    def __init__(self, path: Path):
        self.stream = path.open("a+b")
        self.owned = False

    def acquire(self) -> bool:
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owned = True
        except OSError:
            return False
        return True

    def close(self) -> None:
        self.stream.close()


def existing_address(path: Path) -> str | None:
    """Only reopen our own loopback server, never a URL supplied by a file."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, instance_id = info["port"], info["instance_id"]
        if type(port) is not int or not 1024 <= port <= 65535:
            return None
        if not isinstance(instance_id, str) or len(instance_id) != 32:
            return None
        address = f"http://127.0.0.1:{port}"
        with build_opener(ProxyHandler({})).open(address + "/api/app/identity", timeout=1) as response:
            actual = json.load(response)
        if actual == {"app": "job-radar", "instance_id": instance_id}:
            return address
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def bind_local(preferred_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", preferred_port))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


async def serve(data_dir: Path, port: int, *, open_browser: bool, bridge: bool = True) -> None:
    from aiohttp import web
    from job_radar.server import create_app

    stopped = asyncio.Event()
    instance_id = secrets.token_hex(16)
    sock = bind_local(port)
    port = sock.getsockname()[1]
    address = f"http://127.0.0.1:{port}"
    startup_state = {"message": "正在准备本地 Chrome 连接，请稍后点击刷新连接"} if bridge else {}
    app = create_app(data_dir, packaged=True, instance_id=instance_id, request_quit=stopped.set,
                     startup_state=startup_state)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.SockSite(runner, sock).start()
        info = {"port": port, "instance_id": instance_id, "pid": os.getpid()}
        pending = data_dir / "desktop-instance.tmp"
        pending.write_text(json.dumps(info), encoding="utf-8")
        pending.replace(data_dir / "desktop-instance.json")
        logging.info("Local workspace started at %s", address)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, address)
        if bridge:
            try:
                from job_radar.cli import _start_bridge
                await asyncio.to_thread(_start_bridge)
                startup_state.clear()
            except Exception:
                startup_state["message"] = "本地 Chrome 连接未能启动：请检查 19826 端口是否被其他程序占用，然后重新启动应用"
                logging.exception("Chrome Bridge unavailable; local records and demo remain usable")
        await stopped.wait()
    finally:
        await runner.cleanup()
        sock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Job Radar 本地工作台")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    lock = None
    data_dir = args.data_dir.expanduser().resolve()
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("端口须在 1024 到 65535 之间")
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(data_dir / "desktop.lock")
        if not lock.acquire():
            for _ in range(50):
                address = existing_address(data_dir / "desktop-instance.json")
                if address:
                    if not args.no_open:
                        webbrowser.open(address)
                    return 0
                time.sleep(0.2)
            raise RuntimeError("应用正在启动或退出，请稍等片刻后再次双击启动。")
        handler = RotatingFileHandler(data_dir / "desktop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
        asyncio.run(serve(data_dir, args.port, open_browser=not args.no_open))
        return 0
    except Exception as exc:
        logging.exception("Job Radar startup failed")
        message = f"Job Radar 未能启动：{exc}\n\n请完整解压下载包后重新启动。\n本地日志：{data_dir / 'desktop.log'}"
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Job Radar", 0x10)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
