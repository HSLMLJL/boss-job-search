"""Application services shared by the browser interface and command line."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

from job_radar.config import TaskConfig
from job_radar.exporter import export_run
from job_radar.store import JobStore


@contextmanager
def collection_lock(root: Path):
	"""Cross-process, crash-released lock; viewing and exporting remain available."""
	with (root / "collection.lock").open("a+b") as stream:
		stream.seek(0, 2)
		if stream.tell() == 0:
			stream.write(b"0")
			stream.flush()
		stream.seek(0)
		try:
			if __import__("os").name == "nt":
				import msvcrt

				msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
			else:
				import fcntl

				fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError as exc:
			raise ValueError("已有采集任务运行，请先停止或等待完成") from exc
		try:
			yield
		finally:
			stream.seek(0)
			if __import__("os").name == "nt":
				msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
			else:
				fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def auth_status(data_dir: Path) -> dict:
	try:
		from boss_agent_cli.commands.radar import _radar_extension_data

		state = _radar_extension_data()
		ready = state.get("verified") is True
	except Exception:
		ready = False
	return {
		"ready": ready,
		"message": "Chrome 插件已连接；网页登录与验证状态将在搜索时检查"
		if ready
		else "请运行 uv run python -m job_radar setup，按提示连接 Chrome 扩展并在 Chrome 登录 BOSS；也可以先体验合成示例。",
	}


def execute_run(store: JobStore, run_id: str, *, stop_event: threading.Event | None = None) -> dict:
	from job_radar.runner import run_task
	from job_radar.source import make_source, SourceAuthRequired
	from job_radar.demo import demo_source

	run = store.get_run(run_id)
	if run is None:
		raise KeyError(run_id)
	config = TaskConfig.from_dict(run["config"])
	with collection_lock(store.root):
		try:
			source = demo_source() if run.get("demo") else make_source(store.root.parent)
			run_task(config, store, source, run_id=run_id, stop_event=stop_event)
		except SourceAuthRequired:
			store.update_run(
				run_id,
				status="auth_required",
				message="Chrome 插件或网页登录不可用，请检查连接并完成 BOSS 登录后继续。",
			)
			raise
		except Exception:
			store.update_run(run_id, status="failed", message="采集未完成，已保存的结果仍可查看。请检查连接后重试。")
			raise
		finally:
			# Always deliver checkpointed rows after collection finishes. The flag
			# protects concurrent readers only while the export files are written.
			store.update_run(run_id, export_pending=True)
			try:
				export_run(store, run_id)
			finally:
				store.update_run(run_id, export_pending=False)
	return store.get_run(run_id)


class Workspace:
	def __init__(self, data_dir: Path):
		self.store = JobStore(data_dir)
		self.active: dict[str, tuple[threading.Thread, threading.Event]] = {}
		self._guard = threading.Lock()
		# Only recover orphaned runs when no other CLI or workspace owns the lock.
		try:
			with collection_lock(self.store.root):
				for run in self.store.list_runs():
					if run["status"] in {"queued", "running"}:
						self.store.update_run(
							run["id"],
							status="stopped",
							export_pending=False,
							message="上次运行已中断，已保存的进度可继续。",
						)
					elif run.get("export_pending"):
						self.store.update_run(
							run["id"],
							export_pending=False,
							message=run.get("message", "") + "；上次导出未完成，可重新导出。",
						)
		except ValueError:
			pass

	def start(self, config: TaskConfig, *, demo: bool = False, run_id: str | None = None) -> dict:
		with self._guard:
			if any(thread.is_alive() for thread, _ in self.active.values()):
				raise ValueError("已有任务正在采集，请先停止或等待完成")
			if run_id:
				run = self.store.get_run(run_id)
				if run is None:
					raise KeyError(run_id)
				if run["status"] == "completed":
					raise ValueError("此任务已经完成。如需重新搜索，请开始一个新任务")
			else:
				run = self.store.create_run(config.to_dict(), demo=demo)
			event = threading.Event()
			thread = threading.Thread(
				target=self._execute, args=(run["id"], event), daemon=True, name="job-radar-collection"
			)
			self.active[run["id"]] = (thread, event)
			thread.start()
			return run

	def _execute(self, run_id: str, event: threading.Event):
		try:
			execute_run(self.store, run_id, stop_event=event)
		except ValueError as exc:
			self.store.update_run(run_id, status="failed", message=str(exc))
		except Exception:
			current = self.store.get_run(run_id)
			if current and current["status"] not in {"failed", "auth_required", "risk_stopped"}:
				self.store.update_run(run_id, status="failed", message="任务未完成。请检查本地登录与浏览器配置后重试。")

	def stop(self, run_id: str) -> dict:
		if self.store.get_run(run_id) is None:
			raise KeyError(run_id)
		pair = self.active.get(run_id)
		if pair and pair[0].is_alive():
			pair[1].set()
			return self.store.update_run(run_id, message="正在停止：当前请求完成后保存进度")
		return self.store.get_run(run_id)

	def shutdown(self):
		for thread, event in self.active.values():
			if thread.is_alive():
				event.set()
