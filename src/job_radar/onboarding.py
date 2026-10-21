"""Small, dependency-light setup helpers for the local job-search product."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import webbrowser


def bundled_bridge_extension_path() -> Path:
    import boss_agent_cli

    package = Path(boss_agent_cli.__file__).resolve().parent
    candidates = [package / "bridge_extension", package.parent.parent / "extension"]
    for path in candidates:
        if (path / "manifest.json").is_file():
            return path
    raise FileNotFoundError("安装包缺少 Chrome 扩展，请重新完整解压下载包")


def extension_data() -> dict:
    """Check one local status response; never make a platform request."""
    from boss_agent_cli.bridge.client import BridgeClient

    path = bundled_bridge_extension_path()
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not isinstance(version, str) or not version:
        raise ValueError("扩展缺少版本信息")
    digest = hashlib.sha256((path / "background.js").read_bytes()).hexdigest()
    status = BridgeClient().status() or {}
    extension_id = status.get("extensionId")
    valid_id = isinstance(extension_id, str) and re.fullmatch(r"[a-p]{32}", extension_id)
    return {
        "verified": bool(status.get("extensionConnected") is True and valid_id
                         and status.get("extensionVersion") == version
                         and status.get("extensionBuildHash") == digest),
        "expected_path": str(path),
        "expected_version": version,
        "extension_connected": status.get("extensionConnected") is True,
    }


def install_extension_path() -> Path:
    bundled = bundled_bridge_extension_path()
    # The portable ZIP offers a short, easy-to-select copy beside its launcher.
    outer = bundled.parents[2] / "extension"
    if (outer / "manifest.json").is_file():
        expected = ["manifest.json", "background.js"]
        if all((outer / name).read_bytes() == (bundled / name).read_bytes() for name in expected):
            return outer
    return bundled


def open_extension_folder() -> None:
    path = install_extension_path()
    if os.name == "nt":
        os.startfile(str(path))
    else:
        webbrowser.open(path.as_uri())


def open_boss() -> None:
    url = "https://www.zhipin.com/web/geek/jobs"
    if os.name == "nt":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                chrome = Path(root) / "Google/Chrome/Application/chrome.exe"
                if chrome.is_file():
                    subprocess.Popen([str(chrome), url])
                    return
    if not webbrowser.open(url):
        raise ValueError("未找到浏览器，请手动在已加载扩展的 Chrome 打开 BOSS 直聘")
