from __future__ import annotations

import importlib.util
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_windows_portable.py"
SPEC = importlib.util.spec_from_file_location("portable_builder", SCRIPT)
assert SPEC and SPEC.loader
portable_builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portable_builder)


def write_runtime_zip(path: Path) -> Path:
	with zipfile.ZipFile(path, "w") as archive:
		archive.writestr("python.exe", b"python")
		archive.writestr("pythonw.exe", b"pythonw")
		archive.writestr("python313._pth", "python313.zip\n")
	return path


def test_runtime_rejects_unpinned_or_incomplete_input(tmp_path, monkeypatch):
	runtime = write_runtime_zip(tmp_path / "runtime.zip")
	monkeypatch.setattr(portable_builder, "RUNTIME_SHA256", portable_builder.sha256(runtime))
	portable_builder.verify_runtime(runtime)
	with zipfile.ZipFile(tmp_path / "bad.zip", "w") as archive:
		archive.writestr("python.exe", b"python")
	monkeypatch.setattr(portable_builder, "RUNTIME_SHA256", portable_builder.sha256(tmp_path / "bad.zip"))
	with pytest.raises(portable_builder.PortableBuildError, match="embedded"):
		portable_builder.verify_runtime(tmp_path / "bad.zip")


def test_source_copy_keeps_extension_in_user_and_package_locations(tmp_path):
	root = tmp_path / "repo"
	(root / "src" / "job_radar").mkdir(parents=True)
	(root / "src" / "job_radar" / "desktop.py").write_text("", encoding="utf-8")
	(root / "src" / "boss_agent_cli").mkdir(parents=True)
	(root / "src" / "boss_agent_cli" / "__init__.py").write_text("", encoding="utf-8")
	(root / "extension").mkdir()
	(root / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
	(root / "packaging" / "windows").mkdir(parents=True)
	for name in ("Start-Job-Radar.cmd", "PORTABLE-README.txt"):
		(root / "packaging" / "windows" / name).write_text(name, encoding="utf-8")
	for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
		(root / name).write_text(name, encoding="utf-8")
	bundle = tmp_path / "bundle"
	bundle.mkdir()

	portable_builder.copy_public_application(root, bundle)

	assert (bundle / "extension" / "manifest.json").read_text(encoding="utf-8") == "{}"
	assert (bundle / "app" / "boss_agent_cli" / "bridge_extension" / "manifest.json").is_file()
	assert not (bundle / "app" / ".git").exists()


def test_launcher_and_requirements_match_portable_contract():
	launcher = (ROOT / "packaging" / "windows" / "Start-Job-Radar.cmd").read_text(encoding="utf-8")
	requirements = (ROOT / "packaging" / "windows" / "requirements-portable.txt").read_text(encoding="utf-8")
	assert 'start "" /d "%ROOT%" "%ROOT%runtime\\pythonw.exe" -m job_radar.desktop %*' in launcher
	assert 'if not exist "%ROOT%app\\job_radar\\desktop.py"' in launcher
	for requirement in ("aiohttp==", "click==", "httpx[socks]==", "openpyxl==", "PyYAML=="):
		assert requirement in requirements


def test_write_pth_isolated_to_bundle_app_and_vendor(tmp_path):
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	pth = runtime / "python313._pth"
	pth.write_text("old", encoding="utf-8")

	portable_builder.write_pth(runtime)

	assert pth.read_text(encoding="utf-8") == "python313.zip\n.\n../app\n../app/vendor\nimport site\n"
