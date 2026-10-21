from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any


class RadarRunInProgressError(RuntimeError):
	def __init__(self, holder: dict[str, Any] | None = None) -> None:
		self.holder = holder or {}
		profile = str(self.holder.get("profile") or "未知配置")
		started_at = str(self.holder.get("started_at") or "未知时间")
		super().__init__(f"已有岗位雷达正在运行（配置: {profile}，开始时间: {started_at}）")


class RadarRunLock:
	"""Cross-process single-run lock released automatically when the process exits."""

	def __init__(self, path: Path, *, profile: str) -> None:
		self.path = path
		self.profile = profile
		self._handle: Any = None

	def __enter__(self) -> RadarRunLock:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		handle = self.path.open("a+b")
		if self.path.stat().st_size == 0:
			handle.write(b"0")
			handle.flush()
		handle.seek(0)
		try:
			_lock_nonblocking(handle)
		except OSError as exc:
			holder = _read_holder(handle)
			handle.close()
			raise RadarRunInProgressError(holder) from exc
		self._handle = handle
		metadata = {
			"pid": os.getpid(),
			"profile": self.profile,
			"started_at": datetime.now(timezone.utc).isoformat(),
		}
		handle.seek(0)
		handle.truncate()
		handle.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"))
		handle.flush()
		return self

	def __exit__(
		self,
		exc_type: type[BaseException] | None,
		exc_value: BaseException | None,
		traceback: TracebackType | None,
	) -> None:
		if self._handle is None:
			return
		try:
			self._handle.seek(0)
			_unlock(self._handle)
		finally:
			self._handle.close()
			self._handle = None


def _read_holder(handle: Any) -> dict[str, Any]:
	try:
		handle.seek(0)
		value = json.loads(handle.read().decode("utf-8"))
	except (OSError, UnicodeDecodeError, ValueError):
		return {}
	return value if isinstance(value, dict) else {}


def _lock_nonblocking(handle: Any) -> None:
	if os.name == "nt":
		import msvcrt

		msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
		return
	fcntl_module: Any = __import__("fcntl")
	fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_EX | fcntl_module.LOCK_NB)


def _unlock(handle: Any) -> None:
	if os.name == "nt":
		import msvcrt

		msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
		return
	fcntl_module: Any = __import__("fcntl")
	fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
