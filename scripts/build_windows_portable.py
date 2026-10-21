"""Build the self-contained, auditable Windows x64 Job Radar ZIP.

The release deliberately vendors Python packages beside the official CPython
embedded distribution instead of creating a PyInstaller executable.  This keeps
the runtime traceable to python.org and avoids relying on a newly-generated,
unsigned launcher executable on managed Windows devices.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable
from urllib.request import urlopen
import zipfile


PYTHON_VERSION = "3.13.13"
RUNTIME_RELEASE_PAGE = "https://www.python.org/downloads/release/python-31313/"
RUNTIME_URL = "https://www.python.org/ftp/python/3.13.13/python-3.13.13-embed-amd64.zip"
# Published by the Python 3.13.13 release page. Keep the value literal so a
# changed upstream download cannot silently become a release runtime.
RUNTIME_SHA256 = "8766a8775746235e23cf5aee5027ab1060bb981d93110577adcf3508aa0cbd55"
PACKAGE_NAME = "Job-Radar-0.2.0-Windows-x64"

PRIVATE_NAMES = {
	".git",
	".venv",
	".pytest_cache",
	".ruff_cache",
	"__pycache__",
	".boss-agent",
	"work",
	"dist",
	"build",
}


class PortableBuildError(RuntimeError):
	"""A controlled build input or output did not meet the release contract."""


def repo_root() -> Path:
	return Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for block in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(block)
	return digest.hexdigest()


def safe_copytree(source: Path, destination: Path) -> None:
	if not source.is_dir():
		raise PortableBuildError(f"Required public source is missing: {source.name}")

	def ignored(_directory: str, names: list[str]) -> set[str]:
		return {name for name in names if name in PRIVATE_NAMES or name.endswith(".pyc")}

	shutil.copytree(source, destination, ignore=ignored)


def download_runtime(destination: Path) -> Path:
	"""Fetch the pinned python.org runtime and verify it before extraction."""
	destination.parent.mkdir(parents=True, exist_ok=True)
	if destination.is_file() and sha256(destination) == RUNTIME_SHA256:
		return destination
	temporary = destination.with_suffix(destination.suffix + ".part")
	temporary.unlink(missing_ok=True)
	try:
		with urlopen(RUNTIME_URL, timeout=90) as response, temporary.open("wb") as output:
			shutil.copyfileobj(response, output)
	except OSError as exc:
		temporary.unlink(missing_ok=True)
		raise PortableBuildError("Unable to download the official CPython runtime") from exc
	if sha256(temporary) != RUNTIME_SHA256:
		temporary.unlink(missing_ok=True)
		raise PortableBuildError("Official CPython runtime SHA-256 verification failed")
	temporary.replace(destination)
	return destination


def verify_runtime(runtime_zip: Path) -> None:
	if not runtime_zip.is_file():
		raise PortableBuildError("Provide --runtime-zip or allow the official runtime download")
	if sha256(runtime_zip) != RUNTIME_SHA256:
		raise PortableBuildError("Runtime SHA-256 does not match CPython 3.13.13 x64")
	try:
		with zipfile.ZipFile(runtime_zip) as archive:
			names = set(archive.namelist())
	except zipfile.BadZipFile as exc:
		raise PortableBuildError("Runtime file is not a valid ZIP") from exc
	if not {"python.exe", "pythonw.exe"}.issubset(names):
		raise PortableBuildError("Runtime ZIP is not the CPython embedded x64 distribution")


def requirements_path(root: Path) -> Path:
	return root / "packaging" / "windows" / "requirements-portable.txt"


def run(command: list[str], *, cwd: Path) -> None:
	try:
		subprocess.run(command, cwd=cwd, check=True)
	except subprocess.CalledProcessError as exc:
		raise PortableBuildError("Dependency preparation failed") from exc


def download_wheels(requirements: Path, wheel_dir: Path, *, refresh: bool) -> None:
	wheel_dir.mkdir(parents=True, exist_ok=True)
	existing = list(wheel_dir.glob("*.whl"))
	if existing and not refresh:
		return
	if refresh:
		for wheel in existing:
			wheel.unlink()
	run(
		[
			sys.executable,
			"-m",
			"pip",
			"download",
			"--dest",
			str(wheel_dir),
			"--only-binary=:all:",
			"--implementation",
			"cp",
			"--platform",
			"win_amd64",
			"--python-version",
			"313",
			"--abi",
			"cp313",
			"-r",
			str(requirements),
		],
		cwd=requirements.parent,
	)


def install_wheels(requirements: Path, wheel_dir: Path, vendor_dir: Path) -> None:
	vendor_dir.mkdir(parents=True, exist_ok=True)
	run(
		[
			sys.executable,
			"-m",
			"pip",
			"install",
			"--no-index",
			"--find-links",
			str(wheel_dir),
			"--only-binary=:all:",
			"--target",
			str(vendor_dir),
			"--requirement",
			str(requirements),
		],
		cwd=requirements.parent,
	)


def write_pth(runtime_dir: Path) -> None:
	pth_files = list(runtime_dir.glob("python*._pth"))
	if len(pth_files) != 1:
		raise PortableBuildError("Embedded runtime must contain exactly one python*._pth file")
	pth_files[0].write_text("python313.zip\n.\n../app\n../app/vendor\nimport site\n", encoding="utf-8")


def copy_public_application(root: Path, bundle: Path) -> None:
	app = bundle / "app"
	safe_copytree(root / "src" / "job_radar", app / "job_radar")
	# The source package is small (about 4 MiB) and its explicit inclusion avoids
	# PyInstaller-style hidden-import guessing. Only Job Radar's entry point is
	# exposed by the portable launcher.
	safe_copytree(root / "src" / "boss_agent_cli", app / "boss_agent_cli")
	safe_copytree(root / "extension", bundle / "extension")
	safe_copytree(root / "extension", app / "boss_agent_cli" / "bridge_extension")
	for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
		source = root / name
		if not source.is_file():
			raise PortableBuildError(f"Required public notice is missing: {name}")
		shutil.copy2(source, bundle / name)
	shutil.copy2(root / "packaging" / "windows" / "Start-Job-Radar.cmd", bundle / "Start-Job-Radar.cmd")
	shutil.copy2(root / "packaging" / "windows" / "PORTABLE-README.txt", bundle / "PORTABLE-README.txt")


def verify_bundle(bundle: Path) -> None:
	expected = [
		bundle / "runtime" / "python.exe",
		bundle / "runtime" / "pythonw.exe",
		bundle / "app" / "job_radar" / "desktop.py",
		bundle / "app" / "boss_agent_cli" / "bridge_extension" / "manifest.json",
		bundle / "extension" / "manifest.json",
		bundle / "Start-Job-Radar.cmd",
	]
	missing = [path.relative_to(bundle).as_posix() for path in expected if not path.is_file()]
	if missing:
		raise PortableBuildError("Portable bundle is incomplete: " + ", ".join(missing))
	if (bundle / ".boss-agent").exists() or (bundle / ".git").exists():
		raise PortableBuildError("Portable bundle contains prohibited local data")


def manifest(bundle: Path, runtime_zip: Path, wheel_dir: Path, requirements: Path) -> dict[str, object]:
	files = []
	for path in sorted(bundle.rglob("*")):
		if path.is_file() and path.name != "PORTABLE-MANIFEST.json":
			files.append({"path": path.relative_to(bundle).as_posix(), "sha256": sha256(path)})
	wheels = [{"name": path.name, "sha256": sha256(path)} for path in sorted(wheel_dir.glob("*.whl"))]
	return {
		"format": 1,
		"package": PACKAGE_NAME,
		"runtime": {
			"source": RUNTIME_URL,
			"version": PYTHON_VERSION,
			"sha256": sha256(runtime_zip),
			"release_page": RUNTIME_RELEASE_PAGE,
			"verification": "SHA-256 pinned from the official python.org 3.13.13 release page",
		},
		"requirements": requirements.read_text(encoding="utf-8").splitlines(),
		"wheels": wheels,
		"files": files,
	}


def archive(bundle: Path, output: Path) -> Path:
	output.parent.mkdir(parents=True, exist_ok=True)
	output.unlink(missing_ok=True)
	with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as result:
		for path in sorted(bundle.rglob("*")):
			if path.is_file():
				result.write(path, f"{PACKAGE_NAME}/{path.relative_to(bundle).as_posix()}")
	return output


def build_portable(
	*,
	root: Path,
	runtime_zip: Path,
	wheel_dir: Path,
	output: Path,
	refresh_wheels: bool = False,
) -> Path:
	root = root.resolve()
	runtime_zip = runtime_zip.resolve()
	wheel_dir = wheel_dir.resolve()
	output = output.resolve()
	requirements = requirements_path(root)
	if not requirements.is_file():
		raise PortableBuildError("Portable requirements file is missing")
	verify_runtime(runtime_zip)
	download_wheels(requirements, wheel_dir, refresh=refresh_wheels)
	with tempfile.TemporaryDirectory(prefix="job-radar-portable-") as temporary:
		stage = Path(temporary)
		bundle = stage / PACKAGE_NAME
		bundle.mkdir()
		runtime = bundle / "runtime"
		with zipfile.ZipFile(runtime_zip) as source:
			source.extractall(runtime)
		write_pth(runtime)
		copy_public_application(root, bundle)
		install_wheels(requirements, wheel_dir, bundle / "app" / "vendor")
		verify_bundle(bundle)
		(bundle / "PORTABLE-MANIFEST.json").write_text(
			json.dumps(manifest(bundle, runtime_zip, wheel_dir, requirements), ensure_ascii=False, indent=2) + "\n",
			encoding="utf-8",
		)
		return archive(bundle, output)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
	root = repo_root()
	parser = argparse.ArgumentParser(description="Build the Job Radar portable Windows x64 ZIP")
	parser.add_argument("--runtime-zip", type=Path, help="Verified CPython embedded ZIP; default downloads python.org")
	parser.add_argument("--wheel-dir", type=Path, default=root / "work" / "windows-portable-wheels")
	parser.add_argument("--output", type=Path, default=root / "dist" / f"{PACKAGE_NAME}.zip")
	parser.add_argument("--refresh-wheels", action="store_true", help="Discard cached wheels and redownload them")
	return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
	args = parse_args(argv)
	root = repo_root()
	runtime_zip = args.runtime_zip or (root / "work" / "windows-portable-runtime" / "python-3.13.13-embed-amd64.zip")
	try:
		if args.runtime_zip is None:
			runtime_zip = download_runtime(runtime_zip)
		result = build_portable(
			root=root,
			runtime_zip=runtime_zip,
			wheel_dir=args.wheel_dir,
			output=args.output,
			refresh_wheels=args.refresh_wheels,
		)
	except PortableBuildError as exc:
		print(f"Portable build failed: {exc}", file=sys.stderr)
		return 1
	print(result)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
