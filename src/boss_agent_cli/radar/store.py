from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from boss_agent_cli.radar.models import (
	JobObservation,
	RadarEvent,
        is_relevant,
        normalize_relevance_mode,
	observation_json,
	utc_now,
)
from boss_agent_cli.radar_core import normalize_text


_CHINA_TZ = timezone(timedelta(hours=8))


class RadarStore:
	def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
		self._read_only = read_only
		# Repost matching runs once per newly observed job. Cache the logical
		# membership at the physical-run boundary instead of decoding every
		# historical stats_json row for every job on a dense result page.
		self._logical_scan_members_cache: dict[str, tuple[str, frozenset[str]]] = {}
		if read_only:
			self._conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
		else:
			db_path.parent.mkdir(parents=True, exist_ok=True)
			self._conn = sqlite3.connect(db_path)
		self._conn.execute("PRAGMA foreign_keys = ON")
		if not read_only:
			self._conn.execute("PRAGMA journal_mode = WAL")
			self._init_schema()

	def _init_schema(self) -> None:
		self._conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS radar_runs (
				run_id TEXT PRIMARY KEY,
				mode TEXT NOT NULL,
				status TEXT NOT NULL,
				started_at TEXT NOT NULL,
				completed_at TEXT,
				stats_json TEXT NOT NULL DEFAULT '{}'
			);
			CREATE TABLE IF NOT EXISTS radar_jobs (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				platform TEXT NOT NULL,
				identity_key TEXT NOT NULL,
				platform_job_id TEXT NOT NULL DEFAULT '',
				family_key TEXT NOT NULL,
				security_id TEXT NOT NULL DEFAULT '',
				title TEXT NOT NULL,
				company TEXT NOT NULL,
				city TEXT NOT NULL DEFAULT '',
				district TEXT NOT NULL DEFAULT '',
				salary TEXT NOT NULL DEFAULT '',
				experience TEXT NOT NULL DEFAULT '',
				education TEXT NOT NULL DEFAULT '',
				content_hash TEXT NOT NULL,
				published_at TEXT,
				first_seen_at TEXT NOT NULL,
				last_seen_at TEXT NOT NULL,
				first_run_id TEXT NOT NULL,
				last_run_id TEXT NOT NULL,
				payload_json TEXT NOT NULL,
				UNIQUE(platform, identity_key),
				FOREIGN KEY(first_run_id) REFERENCES radar_runs(run_id),
				FOREIGN KEY(last_run_id) REFERENCES radar_runs(run_id)
			);
			CREATE INDEX IF NOT EXISTS radar_jobs_family_idx
				ON radar_jobs(platform, family_key, last_seen_at DESC);
			CREATE INDEX IF NOT EXISTS radar_jobs_first_seen_idx
				ON radar_jobs(first_seen_at DESC);
			CREATE TABLE IF NOT EXISTS radar_job_sources (
				job_pk INTEGER NOT NULL,
				query TEXT NOT NULL,
				shard TEXT NOT NULL,
				first_seen_at TEXT NOT NULL,
				last_seen_at TEXT NOT NULL,
				first_page INTEGER NOT NULL DEFAULT 0,
				last_page INTEGER NOT NULL DEFAULT 0,
				first_rank INTEGER NOT NULL DEFAULT 0,
				last_rank INTEGER NOT NULL DEFAULT 0,
				PRIMARY KEY(job_pk, query, shard),
				FOREIGN KEY(job_pk) REFERENCES radar_jobs(id) ON DELETE CASCADE
			);
			CREATE TABLE IF NOT EXISTS radar_run_observations (
				run_id TEXT NOT NULL,
				job_pk INTEGER NOT NULL,
				query TEXT NOT NULL,
				shard TEXT NOT NULL,
				page INTEGER NOT NULL DEFAULT 0,
				rank INTEGER NOT NULL DEFAULT 0,
				observed_at TEXT NOT NULL,
				PRIMARY KEY(run_id, job_pk, query, shard),
				FOREIGN KEY(run_id) REFERENCES radar_runs(run_id) ON DELETE CASCADE,
				FOREIGN KEY(job_pk) REFERENCES radar_jobs(id) ON DELETE CASCADE
			);
			CREATE INDEX IF NOT EXISTS radar_run_observations_page_idx
				ON radar_run_observations(run_id, shard, query, page, rank);
			CREATE TABLE IF NOT EXISTS radar_page_captures (
				run_id TEXT NOT NULL,
				shard TEXT NOT NULL,
				query TEXT NOT NULL,
				page INTEGER NOT NULL CHECK(page >= 1),
				raw_job_count INTEGER NOT NULL CHECK(raw_job_count >= 0),
				has_more INTEGER NOT NULL CHECK(has_more IN (0, 1)),
				page_size INTEGER CHECK(page_size IS NULL OR page_size >= 1),
				same_page_state INTEGER NOT NULL DEFAULT 0 CHECK(same_page_state IN (0, 1)),
				captured_at TEXT NOT NULL,
				PRIMARY KEY(run_id, shard, query, page),
				FOREIGN KEY(run_id) REFERENCES radar_runs(run_id) ON DELETE CASCADE
			);
			CREATE INDEX IF NOT EXISTS radar_page_captures_search_idx
				ON radar_page_captures(run_id, shard, query, page);
			CREATE TABLE IF NOT EXISTS radar_events (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				run_id TEXT NOT NULL,
				job_pk INTEGER NOT NULL,
				event_type TEXT NOT NULL,
				previous_job_id TEXT NOT NULL DEFAULT '',
				created_at TEXT NOT NULL,
				UNIQUE(run_id, job_pk, event_type),
				FOREIGN KEY(run_id) REFERENCES radar_runs(run_id) ON DELETE CASCADE,
				FOREIGN KEY(job_pk) REFERENCES radar_jobs(id) ON DELETE CASCADE
			);
			CREATE INDEX IF NOT EXISTS radar_events_run_idx
				ON radar_events(run_id, event_type);
			CREATE TABLE IF NOT EXISTS radar_baseline_progress (
				profile_fingerprint TEXT NOT NULL,
				search_key TEXT NOT NULL,
				completed_at TEXT NOT NULL,
				PRIMARY KEY(profile_fingerprint, search_key)
			);
			CREATE TABLE IF NOT EXISTS radar_validation_reviews (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				profile_fingerprint TEXT NOT NULL,
				metric TEXT NOT NULL CHECK(metric IN ('relevance', 'recall', 'old_newness')),
				true_count INTEGER NOT NULL CHECK(true_count >= 0),
				false_count INTEGER NOT NULL CHECK(false_count >= 0),
				review_date TEXT NOT NULL,
				note TEXT NOT NULL DEFAULT '',
				created_at TEXT NOT NULL
			);
			CREATE INDEX IF NOT EXISTS radar_validation_reviews_profile_idx
				ON radar_validation_reviews(profile_fingerprint, metric, review_date DESC);
			"""
		)
		source_columns = {
			str(row[1]) for row in self._conn.execute("PRAGMA table_info(radar_job_sources)").fetchall()
		}
		for column, definition in (
			("first_page", "INTEGER NOT NULL DEFAULT 0"),
			("last_page", "INTEGER NOT NULL DEFAULT 0"),
			("first_rank", "INTEGER NOT NULL DEFAULT 0"),
			("last_rank", "INTEGER NOT NULL DEFAULT 0"),
		):
			if column not in source_columns:
				self._conn.execute(f"ALTER TABLE radar_job_sources ADD COLUMN {column} {definition}")
		# securityId is a short-lived access credential, not a job identity. Older
		# development databases may contain it, so purge it during the idempotent
		# schema migration and never persist new values below.
		self._conn.execute("UPDATE radar_jobs SET security_id = '' WHERE security_id <> ''")
		self._conn.commit()

	def has_jobs(self) -> bool:
		row = self._conn.execute("SELECT 1 FROM radar_jobs LIMIT 1").fetchone()
		return row is not None

	def has_completed_baseline(self, profile_fingerprint: str | None = None) -> bool:
		rows = self._conn.execute(
			"""
			SELECT stats_json FROM radar_runs
			WHERE mode = 'baseline' AND status = 'completed'
			ORDER BY completed_at DESC
			"""
		).fetchall()
		for row in rows:
			try:
				stats = json.loads(row[0])
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict):
				continue
			same_profile = profile_fingerprint is None or stats.get("profile_fingerprint") == profile_fingerprint
			if "complete" in stats:
				complete = bool(stats.get("complete"))
			else:
				complete = not stats.get("truncated_queries")
			if same_profile and complete and not _unverified_page_capture_completion(stats):
				return True
		return False

	def begin_run(self, mode: str) -> str:
		if mode not in {"baseline", "daily", "reconcile"}:
			raise ValueError(f"unsupported radar mode: {mode}")
		# One RadarStore normally owns one active scanner. A new physical run is a
		# natural cache boundary, so do not retain per-run membership indefinitely
		# in a long-lived CLI/daemon process.
		self._logical_scan_members_cache.clear()
		run_id = uuid.uuid4().hex
		self._conn.execute(
			"INSERT INTO radar_runs (run_id, mode, status, started_at) VALUES (?, ?, 'running', ?)",
			(run_id, mode, utc_now().isoformat()),
		)
		self._conn.commit()
		return run_id

	def update_running_run(self, run_id: str, stats: dict[str, Any]) -> None:
		"""Persist resumable progress without changing the run's terminal state."""
		self._invalidate_logical_scan_members_cache(run_id, stats)
		cursor = self._conn.execute(
			"UPDATE radar_runs SET stats_json = ? WHERE run_id = ? AND status = 'running'",
			(json.dumps(stats, ensure_ascii=False, sort_keys=True), run_id),
		)
		if cursor.rowcount != 1:
			self._conn.rollback()
			raise KeyError(run_id)
		self._conn.commit()

	def recover_running_runs(self) -> list[str]:
		"""Mark abandoned rows terminal after the caller acquired the process lock."""
		rows = self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs WHERE status = 'running' ORDER BY started_at"
		).fetchall()
		if not rows:
			return []
		now = utc_now().isoformat()
		recovered: list[str] = []
		with self._conn:
			for run_id, raw_stats in rows:
				try:
					stats = json.loads(str(raw_stats))
				except (TypeError, ValueError):
					stats = {}
				if not isinstance(stats, dict):
					stats = {}
				stats["complete"] = False
				stats["stop_code"] = "PROCESS_INTERRUPTED"
				stats["stop_reason"] = "上次雷达进程未正常结束；已保留检查点并允许安全续跑"
				self._conn.execute(
					"UPDATE radar_runs SET status = 'failed', completed_at = ?, stats_json = ? WHERE run_id = ?",
					(now, json.dumps(stats, ensure_ascii=False, sort_keys=True), str(run_id)),
				)
				recovered.append(str(run_id))
		return recovered

	def completed_baseline_searches(self, profile_fingerprint: str) -> set[str]:
		rows = self._conn.execute(
			"SELECT search_key, completed_at FROM radar_baseline_progress WHERE profile_fingerprint = ?",
			(profile_fingerprint,),
		).fetchall()
		invalid_runs = self._conn.execute(
			"SELECT completed_at, stats_json FROM radar_runs WHERE mode = 'baseline' "
			"AND status = 'completed' ORDER BY completed_at DESC",
		).fetchall()
		invalid_after: dict[str, str | None] = {}
		for completed_at, raw_stats in invalid_runs:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if (
				not isinstance(stats, dict)
				or stats.get("profile_fingerprint") != profile_fingerprint
				or not _unverified_page_capture_completion(stats)
			):
				continue
			for key in _string_set(stats.get("completed_searches")):
				previous = invalid_after.get(key)
				if previous is None or (isinstance(completed_at, str) and completed_at > previous):
					invalid_after[key] = str(completed_at) if completed_at else None
		return {
			str(key)
			for key, marked_at in rows
			if str(key) not in invalid_after
			or not isinstance(marked_at, str)
			or invalid_after[str(key)] is not None and marked_at > invalid_after[str(key)]
		}

	def resumable_baseline_context(self, profile_fingerprint: str) -> tuple[set[str], str, str]:
		"""Return completed searches, logical id, and reason for the current baseline campaign."""
		completed = self.completed_baseline_searches(profile_fingerprint)
		rows = self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs WHERE mode = 'baseline' "
			"AND status IN ('stopped', 'failed') ORDER BY completed_at DESC, started_at DESC",
		).fetchall()
		for run_id, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			if not _safe_budget_checkpoint(stats):
				return set(), "", ""
			return (
				completed,
				str(stats.get("logical_scan_id") or run_id),
				str(stats.get("baseline_reason") or "initial"),
			)
		return completed, "", ""

	def explicit_visible_continuation_context(
		self,
		*,
		run_id: str,
		profile_fingerprint: str,
                max_minutes: int = 720,
		now: datetime | None = None,
	) -> dict[str, Any]:
		"""Validate one exact visible-page refusal without repairing its old run."""
		if not run_id or type(max_minutes) is not int or max_minutes < 1:
			raise ValueError("可见页接续锚点或时间窗口无效")
		runs = self.profile_runs(profile_fingerprint=profile_fingerprint, mode="baseline")
		if not runs:
			raise ValueError("可见页接续缺少此配置的 baseline 失败段")
		anchor_index = next((index for index, item in enumerate(runs) if item["run_id"] == run_id), None)
		if anchor_index is None:
			raise ValueError("可见页接续必须指定此配置已有的 baseline 失败段")
		anchor = runs[anchor_index]
		stats = anchor["stats"]
		if anchor["status"] != "stopped" or bool(stats.get("complete")):
			raise ValueError("可见页接续锚点必须是未完成的 stopped baseline 段")
		reason = stats.get("stop_reason")
		visible_refusal = "Bridge page capture 失败: Visible page is not currently visible; refusing any load-more action"
		visible_inactive_claimed_tab_refusal = (
			"Bridge page capture 失败: Visible page is not currently visible and the "
			"claimed radar tab is not active; refusing any load-more action"
		)
		extension_identity_refusal = "Bridge page capture 失败: Radar extension integrity handshake is missing or mismatched"
		protocol_field_refusal = "Bridge page capture 失败: capture-visible-jobs accepts only fixed search and paging fields"
		extension_sequence_refusal = "Bridge page capture 失败: Visible capture sequence is not established on this extension connection; start at page 1"
		extension_memory_sequence_refusal = (
			"Bridge page capture 失败: Visible capture must be sequential and start at page 1 "
			"(checkpoint=memory)"
		)
		scroll_no_settle_prefix = "Bridge page capture 失败: Visible load-more did not settle; execution result unknown (scroll;"
		scroll_direct_no_settle_prefix = "Bridge page capture 失败: Visible load-more did not settle; execution result unknown (scroll-direct;"
		# Both visibility messages are exact extension no-action refusals. Do not
		# broaden this to arbitrary visibility errors: the continuation still
		# requires a complete saved page chain and a fixed live-page preflight.
		allowed_reasons = {
			visible_refusal,
			visible_inactive_claimed_tab_refusal,
			extension_identity_refusal,
		}
		anchor_scroll_no_settle = (
				isinstance(reason, str)
				and (
						(
								reason.startswith(scroll_no_settle_prefix)
								and "has not reached the requested page" in reason
						)
						or (
								reason.startswith(scroll_direct_no_settle_prefix)
								and "Visible BOSS page is not a settled result page: job list is still loading" in reason
						)
				)
		)
		if (
				stats.get("stop_code") != "BROWSER_ERROR"
				or not isinstance(reason, str)
				or (reason not in allowed_reasons and not anchor_scroll_no_settle)
				or stats.get("operator_handoff")
		):
			raise ValueError("本地零动作接续仅允许明确的页面不可见或扩展完整性拒绝")
		failed = stats.get("failed_searches")
		progress = stats.get("search_progress")
		completed = stats.get("completed_searches")
		if not isinstance(failed, list) or len(failed) != 1 or not isinstance(progress, dict) or len(progress) != 1 or not isinstance(completed, list):
			raise ValueError("可见页接续锚点缺少唯一失败搜索或检查点")
		key, checkpoint = next(iter(progress.items()))
		if not isinstance(key, str) or not isinstance(checkpoint, dict):
			raise ValueError("可见页接续检查点格式无效")
		next_page = _positive_int(checkpoint.get("next_page"))
		source_rank = _nonnegative_int(checkpoint.get("source_rank"))
		if next_page is None or next_page < 2 or source_rank is None or failed[0] != f"{key}: BROWSER_ERROR {reason}":
			raise ValueError("本地零动作接续检查点与拒绝原因不一致")
		try:
			ended = datetime.fromisoformat(str(anchor["completed_at"]).replace("Z", "+00:00"))
			current = now or utc_now()
		except (TypeError, ValueError) as exc:
			raise ValueError("可见页接续锚点结束时间不可解析") from exc
		if ended.tzinfo is None or current.tzinfo is None or current < ended or current - ended > timedelta(minutes=max_minutes):
			raise ValueError(f"可见页接续已超出 {max_minutes} 分钟严格窗口")
		if self.completed_baseline_searches(profile_fingerprint) != {str(item) for item in completed if isinstance(item, str)}:
			raise ValueError("可见页接续的已完成 baseline 标记与锚点不一致")
		parts = key.split(" / ", 1)
		if len(parts) != 2:
			raise ValueError("可见页接续搜索键格式无效")
		# An immediately newer segment can exist when an older in-memory
		# daemon rejects a newly-added *local* command field before it ever
		# forwards a command to the extension, or a reloaded extension reports
		# that its in-memory page-one checkpoint has not been re-established.
		# Neither case adds a page capture or observation, so it is not a
		# competing scan.  Preserve that audit row and permit the caller to
		# resume from the prior, fully-proven anchor only when every intervening
		# segment has this exact zero-action shape.
		# Any platform result, page capture, observation, changed progress,
		# different error, or handoff remains a hard rejection.
		completed_keys = {str(item) for item in completed if isinstance(item, str)}
		expected_progress = {key: dict(checkpoint)}
		intervening_zero_action_run_ids: list[str] = []
		for later in runs[:anchor_index]:
			later_stats = later.get("stats") if isinstance(later, dict) else None
			if not isinstance(later_stats, dict):
				raise ValueError("可见页接续之后存在不可审计的 baseline 段")
			later_completed = {str(item) for item in later_stats.get("completed_searches", []) if isinstance(item, str)}
			later_progress = later_stats.get("search_progress")
			later_failed = later_stats.get("failed_searches")
			later_reason = later_stats.get("stop_reason")
			later_zero_action_reason = (
				# This exact memory-sequence refusal is allowed only as an
				# intervening zero-action segment after a fully-proven anchor.
				# It is not an allowed anchor reason.
				later_reason in {
					protocol_field_refusal,
					extension_sequence_refusal,
					extension_memory_sequence_refusal,
				}
				or (
					isinstance(later_reason, str)
                                        and (
                                                        (
                                                                        later_reason.startswith(scroll_no_settle_prefix)
                                                                        and "has not reached the requested page" in later_reason
                                                        )
                                                        or (
                                                                        later_reason.startswith(scroll_direct_no_settle_prefix)
                                                                        and "Visible BOSS page is not a settled result page: job list is still loading" in later_reason
                                                        )
                                        )
				)
			)
			capture_count = self._conn.execute(
				"SELECT COUNT(*) FROM radar_page_captures WHERE run_id = ?", (later["run_id"],)
			).fetchone()[0]
			observation_count = self._conn.execute(
				"SELECT COUNT(*) FROM radar_run_observations WHERE run_id = ?", (later["run_id"],)
			).fetchone()[0]
			if (
				later.get("status") != "stopped"
				or bool(later_stats.get("complete"))
				or later_stats.get("stop_code") != "BROWSER_ERROR"
				or not later_zero_action_reason
				or bool(later_stats.get("operator_handoff"))
				or str(later_stats.get("logical_scan_id") or "") != str(stats.get("logical_scan_id") or run_id)
				or later_completed != completed_keys
				or later_progress != expected_progress
				or later_failed != [f"{key}: BROWSER_ERROR {later_stats.get('stop_reason')}"]
				or _nonnegative_int(later_stats.get("pages")) != 0
				or _nonnegative_int(later_stats.get("jobs_seen")) != 0
				or capture_count != 0
				or observation_count != 0
			):
				raise ValueError("可见页接续必须指定最新失败段，除非其后仅有可证实的本地零动作协议拒绝")
			intervening_zero_action_run_ids.append(str(later["run_id"]))
		# A stopped continuation can have saved a later page in its own
		# segment.  Validate the complete, same-logical-scan page chain
		# rather than falsely demanding that every historical page was
		# written by this one physical run.  The sequence below still
		# rejects any duplicate, gap, short nonterminal page, or changed
		# page size before it authorizes the next visible-page action.
		logical_scan_id = str(stats.get("logical_scan_id") or run_id)
		capture_chain_run_ids = [
				str(candidate["run_id"])
				for candidate in runs[anchor_index:]
				if isinstance(candidate.get("stats"), dict)
				and str(candidate["stats"].get("logical_scan_id") or candidate["run_id"])
				== logical_scan_id
				and {
						str(item)
						for item in candidate["stats"].get("completed_searches", [])
						if isinstance(item, str)
				} == completed_keys
		]
		if not capture_chain_run_ids:
			raise ValueError("可见页接续缺少同一观察链路的页面证据")
		placeholders = ", ".join("?" for _ in capture_chain_run_ids)
		rows = self._conn.execute(
				"SELECT page, raw_job_count, has_more, page_size, same_page_state FROM radar_page_captures "
				f"WHERE run_id IN ({placeholders}) AND shard = ? AND query = ? ORDER BY page",
				(*capture_chain_run_ids, parts[0], parts[1]),
		).fetchall()
		expected_pages = list(range(1, next_page))
		if len(rows) != len(expected_pages):
			raise ValueError("可见页接续缺少连续已保存页面证据")
		page_size: int | None = None
		observed_count = 0
		for expected, row in zip(expected_pages, rows):
			page, count, has_more, size, same = row
			if page != expected or has_more != 1 or same != 1 or type(size) is not int or not 1 <= size <= 100 or count != size:
				raise ValueError("可见页接续的历史页面不是连续完整非终页")
			if page_size is None:
				page_size = size
			elif page_size != size:
				raise ValueError("可见页接续的历史页面大小不一致")
			observed_count += count
		if observed_count != source_rank:
			raise ValueError("可见页接续历史页数与 source_rank 不一致")
		return {
			"run_id": run_id,
			"logical_scan_id": str(stats.get("logical_scan_id") or run_id),
			"baseline_reason": str(stats.get("baseline_reason") or "initial"),
			"completed_searches": {str(item) for item in completed if isinstance(item, str)},
			"search_progress": {key: {"next_page": next_page, "source_rank": source_rank}},
			"search_key": key,
			"previous_page": next_page - 1,
                   "page_size": page_size,
                   "job_list_count": observed_count,
                    "intervening_zero_action_run_ids": intervening_zero_action_run_ids,
           }

	def baseline_page_cap_searches(self, profile_fingerprint: str) -> set[str]:
		"""Return completed baseline searches that stopped at their configured page cap."""
		completed = self.completed_baseline_searches(profile_fingerprint)
		if not completed:
			return set()
		rows = self._conn.execute(
			"SELECT stats_json FROM radar_runs WHERE mode = 'baseline' "
			"AND status IN ('completed', 'stopped', 'failed')",
		).fetchall()
		result: set[str] = set()
		for row in rows:
			try:
				stats = json.loads(str(row[0]))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			caps = stats.get("page_cap_reached_searches")
			if isinstance(caps, list):
				result.update(str(item) for item in caps if isinstance(item, str))
		return result & completed

	def resumable_daily_searches(self, profile_fingerprint: str) -> set[str]:
		"""Return completed searches from today's latest interrupted daily run.

		A completed daily run starts a new scan window, so only ``stopped`` or
		``failed`` runs are resumable.  The latest matching run already includes
		any searches carried by an earlier retry, which keeps this read-only and
		avoids a second progress table.
		"""
		completed, _ = self.resumable_daily_context(profile_fingerprint)
		return completed

	def resumable_daily_context(self, profile_fingerprint: str) -> tuple[set[str], str]:
		"""Return completed searches and the logical scan id for today's retry."""
		rows = self._conn.execute(
			"""
			SELECT run_id, status, completed_at, stats_json
			FROM radar_runs
			WHERE mode = 'daily' AND status IN ('completed', 'stopped', 'failed')
			ORDER BY completed_at DESC, started_at DESC
			"""
		).fetchall()
		today = utc_now().astimezone(_CHINA_TZ).date()
		for run_id, status, completed_at, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			if not completed_at:
				return set(), ""
			try:
				completed_date = datetime.fromisoformat(str(completed_at)).astimezone(_CHINA_TZ).date()
			except ValueError:
				return set(), ""
			if completed_date != today or status == "completed" or not _safe_budget_checkpoint(stats):
				return set(), ""
			completed = stats.get("completed_searches")
			if not isinstance(completed, list):
				return set(), ""
			logical_scan_id = str(stats.get("logical_scan_id") or run_id)
			return {str(item) for item in completed if isinstance(item, str)}, logical_scan_id
		return set(), ""

	def explicit_daily_continuation_context(
		self,
		*,
		run_id: str,
		profile_fingerprint: str,
		max_hours: float = 18,
		now: datetime | None = None,
	) -> tuple[set[str], str, datetime]:
		"""Validate one named, bounded daily continuation without a date fallback.

		This is deliberately opt-in: ordinary daily resume remains calendar-day
		strict.  A caller must name the newest stopped segment, so an old
		interruption can never silently become a fresh scan on a later night.
		"""
		if not run_id or max_hours <= 0:
			raise ValueError("续跑锚点或窗口参数无效")
		rows = self._conn.execute(
			"SELECT run_id, status, started_at, completed_at, stats_json FROM radar_runs "
			"WHERE mode = 'daily' AND status IN ('completed', 'stopped', 'failed') "
			"ORDER BY completed_at DESC, started_at DESC"
		).fetchall()
		matching: list[tuple[str, str, str, str, dict[str, Any]]] = []
		for candidate_id, status, started_at, completed_at, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if isinstance(stats, dict) and stats.get("profile_fingerprint") == profile_fingerprint:
				matching.append((str(candidate_id), str(status), str(started_at), str(completed_at or ""), stats))
		if not matching or matching[0][0] != run_id:
			raise ValueError("指定续跑 run 必须是该配置最新的已结束 daily 段")
		candidate_id, status, started_at, _completed_at, stats = matching[0]
		if status != "stopped" or bool(stats.get("complete")):
			raise ValueError("指定续跑 run 必须是未完成的 stopped 段")
		if stats.get("stop_code") or stats.get("stop_reason") or stats.get("operator_handoff") or stats.get("failed_searches"):
			raise ValueError("指定续跑 run 含错误、交接或失败搜索，不能自动接续")
		if int(stats.get("requests") or 0) != 400:
			raise ValueError("指定续跑 run 不是恰好耗尽 400 请求预算的段")
		if not (stats.get("search_progress") or stats.get("truncated_searches")):
			raise ValueError("指定续跑 run 没有可审计的未扫范围")
		logical_scan_id = str(stats.get("logical_scan_id") or candidate_id)
		members = [item for item in matching if str(item[4].get("logical_scan_id") or item[0]) == logical_scan_id]
		try:
			starts = [datetime.fromisoformat(item[2].replace("Z", "+00:00")) for item in members]
		except (AttributeError, TypeError, ValueError) as exc:
			raise ValueError("续跑链缺少可解析的 started_at") from exc
		if any(value.tzinfo is None or value.utcoffset() is None for value in starts):
			raise ValueError("续跑链 started_at 必须带时区")
		logical_started_at = min(starts)
		current = now or utc_now()
		if current.tzinfo is None or current.utcoffset() is None or current - logical_started_at > timedelta(hours=max_hours) or current < logical_started_at:
			raise ValueError(f"指定续跑 run 已超出 {max_hours:g} 小时有界窗口")
		completed = stats.get("completed_searches")
		if not isinstance(completed, list):
			raise ValueError("指定续跑 run 缺少 completed_searches 检查点")
		return {str(item) for item in completed if isinstance(item, str)}, logical_scan_id, logical_started_at

	def default_continuation_blocker(self, profile_fingerprint: str) -> str | None:
		"""Return a reason when implicit resume would reuse an unsafe checkpoint."""
		mode = "daily" if self.has_completed_baseline(profile_fingerprint) else "baseline"
		rows = self._conn.execute(
			"SELECT status, stats_json FROM radar_runs WHERE mode = ? AND status IN ('stopped', 'failed') "
			"ORDER BY completed_at DESC, started_at DESC",
			(mode,),
		).fetchall()
		for status, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			if status != "stopped" or not _safe_budget_checkpoint(stats):
				return "最近中断运行不是纯预算停止（含错误、交接、失败或无有效检查点）；先解决环境后显式使用 --fresh"
			return None
		return None

	def logical_scan_page_cap_searches(self, profile_fingerprint: str, logical_scan_id: str) -> set[str]:
		"""Return page-cap boundaries accumulated by all segments of one logical scan."""
		if not logical_scan_id:
			return set()
		rows = self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs WHERE mode = 'daily' "
			"AND status IN ('completed', 'stopped', 'failed')",
		).fetchall()
		result: set[str] = set()
		for run_id, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			candidate_scan_id = str(stats.get("logical_scan_id") or run_id)
			if candidate_scan_id != logical_scan_id:
				continue
			caps = stats.get("page_cap_reached_searches")
			if isinstance(caps, list):
				result.update(str(item) for item in caps if isinstance(item, str))
		return result

	def logical_scan_run_ids(
		self,
		profile_fingerprint: str,
		logical_scan_id: str,
		*,
		mode: str,
	) -> list[str]:
		"""Return persisted segments of one logical baseline or daily scan in time order."""
		if not logical_scan_id or mode not in {"baseline", "daily"}:
			return []
		rows = self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs WHERE mode = ? "
			"AND status IN ('completed', 'stopped', 'failed') ORDER BY started_at",
			(mode,),
		).fetchall()
		result: list[str] = []
		for run_id, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			candidate_scan_id = str(stats.get("logical_scan_id") or run_id)
			if candidate_scan_id == logical_scan_id:
				result.append(str(run_id))
		return result

	def logical_scan_terminal_full_searches(
		self,
		profile_fingerprint: str,
		logical_scan_id: str,
		*,
		mode: str,
	) -> set[str]:
		"""Return full terminal batches already seen by an interrupted logical scan."""
		if not logical_scan_id or mode not in {"baseline", "daily"}:
			return set()
		rows = self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs WHERE mode = ? "
			"AND status IN ('completed', 'stopped', 'failed')",
			(mode,),
		).fetchall()
		result: set[str] = set()
		for run_id, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			candidate_scan_id = str(stats.get("logical_scan_id") or run_id)
			if candidate_scan_id != logical_scan_id:
				continue
			searches = stats.get("terminal_full_searches")
			if isinstance(searches, list):
				result.update(str(item) for item in searches if isinstance(item, str))
		return result

	def logical_scan_terminal_evidence_missing_searches(
		self,
		profile_fingerprint: str,
		logical_scan_id: str,
		*,
		mode: str,
	) -> set[str]:
		"""Return terminal captures lacking auditable state from a logical scan."""
		if not logical_scan_id or mode not in {"baseline", "daily"}:
			return set()
		rows = self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs WHERE mode = ? "
			"AND status IN ('completed', 'stopped', 'failed')",
			(mode,),
		).fetchall()
		result: set[str] = set()
		for run_id, raw_stats in rows:
			try:
				stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			candidate_scan_id = str(stats.get("logical_scan_id") or run_id)
			if candidate_scan_id != logical_scan_id:
				continue
			searches = stats.get("terminal_evidence_missing_searches")
			if isinstance(searches, list):
				result.update(str(item) for item in searches if isinstance(item, str))
		return result

	def logical_scan_search_progress(
		self,
		profile_fingerprint: str,
		logical_scan_id: str,
		*,
		mode: str,
	) -> dict[str, dict[str, int]]:
		"""Return the next unobserved page for interrupted searches in one scan.

		New runs persist an explicit checkpoint after every successful page.  The
		observation fallback recovers scans created before that field existed, which
		lets an in-progress user database resume without rereading known pages.
		"""
		if not logical_scan_id or mode not in {"baseline", "daily"}:
			return {}
		runs = self.profile_runs(profile_fingerprint=profile_fingerprint, mode=mode)
		matching = [
			run
			for run in reversed(runs)
			if str(run["stats"].get("logical_scan_id") or run["run_id"]) == logical_scan_id
		]
		progress: dict[str, dict[str, int]] = {}
		completed: set[str] = set()
		for run in matching:
			stats = run["stats"]
			raw_progress = stats.get("search_progress")
			if isinstance(raw_progress, dict):
				for key, raw_value in raw_progress.items():
					if not isinstance(key, str) or not isinstance(raw_value, dict):
						continue
					next_page = _positive_int(raw_value.get("next_page"))
					source_rank = _nonnegative_int(raw_value.get("source_rank"))
					if next_page is not None and source_rank is not None:
						progress[key] = {"next_page": next_page, "source_rank": source_rank}
			completed.update(_string_set(stats.get("completed_searches")))

		run_ids = [str(run["run_id"]) for run in matching]
		if run_ids and self._table_exists("radar_run_observations"):
			marks = ",".join("?" for _ in run_ids)
			rows = self._conn.execute(
				f"""
				SELECT shard, query, MAX(page), MAX(rank)
				FROM radar_run_observations
				WHERE run_id IN ({marks}) AND page > 0
				GROUP BY shard, query
				""",
				run_ids,
			).fetchall()
			for shard, query, page, rank in rows:
				key = f"{str(shard)} / {str(query)}"
				if key in completed or key in progress:
					continue
				progress[key] = {
					"next_page": int(page) + 1,
					"source_rank": max(0, int(rank)),
				}
		for key in completed:
			progress.pop(key, None)
		return progress

	def mark_baseline_searches(self, profile_fingerprint: str, search_keys: Iterable[str]) -> None:
		now = utc_now().isoformat()
		with self._conn:
			self._conn.executemany(
				"""
				INSERT INTO radar_baseline_progress (profile_fingerprint, search_key, completed_at)
				VALUES (?, ?, ?)
				ON CONFLICT(profile_fingerprint, search_key)
				DO UPDATE SET completed_at = excluded.completed_at
				""",
				((profile_fingerprint, key, now) for key in search_keys),
			)

	def clear_baseline_progress(self, profile_fingerprint: str) -> None:
		"""Discard only resumable search markers before a deliberate fresh baseline.

		Historical runs, observations, and jobs remain intact for auditing and
		deduplication. The next baseline run must cover every configured search.
		"""
		with self._conn:
			self._conn.execute(
				"DELETE FROM radar_baseline_progress WHERE profile_fingerprint = ?",
				(profile_fingerprint,),
			)

	def list_jobs_for_sources(
			self,
			*,
			queries: Iterable[str],
			shards: Iterable[str],
			relevance_mode: str | None = None,
	) -> list[JobObservation]:
		query_values = tuple(dict.fromkeys(queries))
		shard_values = tuple(dict.fromkeys(shards))
		if not query_values or not shard_values:
			return []
		query_marks = ",".join("?" for _ in query_values)
		shard_marks = ",".join("?" for _ in shard_values)
		rows = self._conn.execute(
			f"""
			SELECT j.platform, MIN(s.query), MIN(s.shard), j.platform_job_id,
				j.title, j.company, j.city, j.district, j.salary, j.experience,
				j.education, j.published_at, j.first_seen_at, j.payload_json
			FROM radar_jobs AS j
			JOIN radar_job_sources AS s ON s.job_pk = j.id
			WHERE s.query IN ({query_marks}) AND s.shard IN ({shard_marks})
			GROUP BY j.id
			ORDER BY j.first_seen_at DESC, j.id DESC
			""",
			(*query_values, *shard_values),
		).fetchall()
		relevance_mode = _resolve_stored_relevance_mode(
			(str(row[13]) for row in rows), requested=relevance_mode
		)
		jobs = [
			(JobObservation(
				platform=str(row[0]),
				query=str(row[1]),
				shard=str(row[2]),
				platform_job_id=str(row[3]),
				title=str(row[4]),
				company=str(row[5]),
				city=str(row[6]),
				district=str(row[7]),
				salary=str(row[8]),
				experience=str(row[9]),
				education=str(row[10]),
					description=" ".join(_stored_relevance_evidence(str(row[13]))),
				published_at=datetime.fromisoformat(str(row[11])) if row[11] else None,
				observed_at=datetime.fromisoformat(str(row[12])),
			), str(row[13]))
			for row in rows
		]
		return [
			job for job, payload_json in jobs
			if _stored_relevance_mode(payload_json) == relevance_mode
			and is_relevant(job, relevance_mode=relevance_mode)
		]

	def list_jobs_for_run(self, run_id: str, *, relevance_mode: str | None = None) -> list[JobObservation]:
		"""Return jobs observed by one run using their persisted first-seen time."""
		return self.list_jobs_for_runs((run_id,), relevance_mode=relevance_mode)

	def list_jobs_for_runs(
			self,
			run_ids: Iterable[str],
		*,
			relevance_mode: str | None = None,
	) -> list[JobObservation]:
		"""Return deduplicated jobs actually observed across selected run segments."""
		ids = tuple(dict.fromkeys(str(run_id) for run_id in run_ids if run_id))
		if not ids:
			return []
		marks = ",".join("?" for _ in ids)
		rows = self._conn.execute(
			f"""
			WITH selected AS (
				SELECT job_pk, MIN(query) AS query, MIN(shard) AS shard
				FROM radar_run_observations
				WHERE run_id IN ({marks})
				GROUP BY job_pk
			)
			SELECT j.platform, j.platform_job_id, j.title, j.company, j.city,
				j.district, j.salary, j.experience, j.education, j.published_at,
				j.first_seen_at, j.payload_json, selected.query, selected.shard
			FROM radar_jobs AS j
			JOIN selected ON selected.job_pk = j.id
			ORDER BY j.first_seen_at DESC, j.id DESC
			""",
			ids,
		).fetchall()
		relevance_mode = _resolve_stored_relevance_mode(
			(str(row[11]) for row in rows), requested=relevance_mode
		)
		jobs = [
			(JobObservation(
				platform=str(row[0]),
				query=str(row[12] or ""),
				shard=str(row[13] or ""),
				platform_job_id=str(row[1]),
				title=str(row[2]),
				company=str(row[3]),
				city=str(row[4]),
				district=str(row[5]),
				salary=str(row[6]),
				experience=str(row[7]),
				education=str(row[8]),
					description=" ".join(_stored_relevance_evidence(str(row[11]))),
				published_at=datetime.fromisoformat(str(row[9])) if row[9] else None,
				observed_at=datetime.fromisoformat(str(row[10])),
			), str(row[11]))
			for row in rows
		]
		return [
			job for job, payload_json in jobs
			if _stored_relevance_mode(payload_json) == relevance_mode
			and is_relevant(job, relevance_mode=relevance_mode)
		]

	def query_overlap_metrics_for_runs(
		self,
		run_ids: Iterable[str],
		*,
		queries: Iterable[str],
		primary_query: str,
	) -> dict[str, dict[str, int]]:
		"""Return exact unique-query contribution counts across scan segments."""
		ids = tuple(dict.fromkeys(str(run_id) for run_id in run_ids if run_id))
		query_values = tuple(dict.fromkeys(str(query) for query in queries if query))
		result = {
			query: {"unique_contributed": 0, "not_in_primary_query": 0}
			for query in query_values
		}
		if not ids or not query_values:
			return result
		marks = ",".join("?" for _ in ids)
		rows = self._conn.execute(
			f"SELECT DISTINCT job_pk, query FROM radar_run_observations WHERE run_id IN ({marks})",
			ids,
		).fetchall()
		queries_by_job: dict[int, set[str]] = {}
		for job_pk, query in rows:
			query_name = str(query)
			if query_name in result:
				queries_by_job.setdefault(int(job_pk), set()).add(query_name)
		for observed_queries in queries_by_job.values():
			if len(observed_queries) == 1:
				result[next(iter(observed_queries))]["unique_contributed"] += 1
			if primary_query not in observed_queries:
				for query in observed_queries:
					result[query]["not_in_primary_query"] += 1
		return result

	def list_new_events_between(
		self,
		*,
		profile_fingerprint: str,
		start: datetime,
		end: datetime,
	) -> list[RadarEvent]:
		"""Return new/reposted events for one profile in a half-open UTC interval."""
		if start.tzinfo is None or end.tzinfo is None:
			raise ValueError("start and end must be timezone-aware")
		if start >= end:
			raise ValueError("start must be earlier than end")
		rows = self._conn.execute(
			"""
			SELECT e.event_type, e.previous_job_id, r.stats_json,
				j.platform, j.platform_job_id, j.title, j.company, j.city,
				j.district, j.salary, j.experience, j.education,
				j.published_at, j.first_seen_at, j.payload_json,
				(SELECT MIN(s.query) FROM radar_job_sources AS s WHERE s.job_pk = j.id),
				(SELECT MIN(s.shard) FROM radar_job_sources AS s WHERE s.job_pk = j.id)
			FROM radar_events AS e
			JOIN radar_runs AS r ON r.run_id = e.run_id
			JOIN radar_jobs AS j ON j.id = e.job_pk
			WHERE e.event_type IN ('new', 'reposted')
				AND e.created_at >= ? AND e.created_at < ?
			ORDER BY e.created_at DESC, e.id DESC
			""",
			(start.isoformat(), end.isoformat()),
		).fetchall()
		result: list[RadarEvent] = []
		for row in rows:
			try:
				stats = json.loads(str(row[2]))
			except (TypeError, ValueError):
				continue
			if stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			job = JobObservation(
				platform=str(row[3]),
				query=str(row[15] or ""),
				shard=str(row[16] or ""),
				platform_job_id=str(row[4]),
				title=str(row[5]),
				company=str(row[6]),
				city=str(row[7]),
				district=str(row[8]),
				salary=str(row[9]),
				experience=str(row[10]),
				education=str(row[11]),
				description=" ".join(_stored_ai_evidence(str(row[14]))),
				published_at=datetime.fromisoformat(str(row[12])) if row[12] else None,
				observed_at=datetime.fromisoformat(str(row[13])),
			)
			result.append(
				RadarEvent(
					event_type=str(row[0]),
					job=job,
					previous_job_id=str(row[1] or ""),
				)
			)
		return result

	def list_events_for_runs(self, run_ids: Iterable[str]) -> list[RadarEvent]:
		"""Return new/repost events belonging only to selected physical runs."""
		ids = tuple(dict.fromkeys(str(run_id) for run_id in run_ids if run_id))
		if not ids:
			return []
		marks = ",".join("?" for _ in ids)
		rows = self._conn.execute(
			f"""
			SELECT e.event_type, e.previous_job_id,
				j.platform, j.platform_job_id, j.title, j.company, j.city,
				j.district, j.salary, j.experience, j.education,
				j.published_at, j.first_seen_at, j.payload_json,
				(SELECT MIN(s.query) FROM radar_job_sources AS s WHERE s.job_pk = j.id),
				(SELECT MIN(s.shard) FROM radar_job_sources AS s WHERE s.job_pk = j.id)
			FROM radar_events AS e
			JOIN radar_jobs AS j ON j.id = e.job_pk
			WHERE e.run_id IN ({marks}) AND e.event_type IN ('new', 'reposted')
			ORDER BY e.created_at DESC, e.id DESC
			""",
			ids,
		).fetchall()
		return [
			RadarEvent(
				event_type=str(row[0]),
				previous_job_id=str(row[1] or ""),
				job=JobObservation(
					platform=str(row[2]),
					platform_job_id=str(row[3]),
					title=str(row[4]),
					company=str(row[5]),
					city=str(row[6]),
					district=str(row[7]),
					salary=str(row[8]),
					experience=str(row[9]),
					education=str(row[10]),
					published_at=datetime.fromisoformat(str(row[11])) if row[11] else None,
					observed_at=datetime.fromisoformat(str(row[12])),
					description=" ".join(_stored_ai_evidence(str(row[13]))),
					query=str(row[14] or ""),
					shard=str(row[15] or ""),
				),
			)
			for row in rows
		]

	def run_page_metrics(self, run_id: str) -> list[dict[str, Any]]:
		"""Summarize unique relevant jobs and new/repost events by run page."""
		if not self._table_exists("radar_run_observations"):
			return []
		rows = self._conn.execute(
			"""
			SELECT o.shard, o.query, o.page,
				COUNT(DISTINCT o.job_pk) AS observed_jobs,
				COUNT(DISTINCT CASE WHEN e.event_type = 'new' THEN o.job_pk END) AS new_jobs,
				COUNT(DISTINCT CASE WHEN e.event_type = 'reposted' THEN o.job_pk END) AS reposted_jobs
			FROM radar_run_observations AS o
			LEFT JOIN radar_events AS e ON e.run_id = o.run_id AND e.job_pk = o.job_pk
			WHERE o.run_id = ?
			GROUP BY o.shard, o.query, o.page
			ORDER BY o.shard, o.query, o.page
			""",
			(run_id,),
		).fetchall()
		return [
			{
				"shard": str(row[0]),
				"query": str(row[1]),
				"page": int(row[2]),
				"observed_jobs": int(row[3]),
				"new_jobs": int(row[4]),
				"reposted_jobs": int(row[5]),
			}
			for row in rows
		]

	def scan_session_page_metrics(self, run_ids: Iterable[str]) -> list[dict[str, Any]]:
		"""Summarize one logical scan, preferring the latest retry position per job/search."""
		ids = tuple(dict.fromkeys(str(run_id) for run_id in run_ids if run_id))
		if not ids or not self._table_exists("radar_run_observations"):
			return []
		marks = ",".join("?" for _ in ids)
		rows = self._conn.execute(
			f"""
			WITH ranked_observations AS (
				SELECT o.run_id, o.job_pk, o.query, o.shard, o.page,
					ROW_NUMBER() OVER (
						PARTITION BY o.job_pk, o.query, o.shard
						ORDER BY COALESCE(r.completed_at, r.started_at) DESC, r.started_at DESC
					) AS retry_rank
				FROM radar_run_observations AS o
				JOIN radar_runs AS r ON r.run_id = o.run_id
				WHERE o.run_id IN ({marks})
			), session_events AS (
				SELECT e.job_pk,
					MAX(CASE WHEN e.event_type = 'new' THEN 1 ELSE 0 END) AS is_new,
					MAX(CASE WHEN e.event_type = 'reposted' THEN 1 ELSE 0 END) AS is_reposted
				FROM radar_events AS e
				WHERE e.run_id IN ({marks})
				GROUP BY e.job_pk
			)
			SELECT o.shard, o.query, o.page,
				COUNT(DISTINCT o.job_pk) AS observed_jobs,
				COUNT(DISTINCT CASE WHEN e.is_new = 1 THEN o.job_pk END) AS new_jobs,
				COUNT(DISTINCT CASE WHEN e.is_reposted = 1 THEN o.job_pk END) AS reposted_jobs
			FROM ranked_observations AS o
			LEFT JOIN session_events AS e ON e.job_pk = o.job_pk
			WHERE o.retry_rank = 1
			GROUP BY o.shard, o.query, o.page
			ORDER BY o.shard, o.query, o.page
			""",
			(*ids, *ids),
		).fetchall()
		return [
			{
				"shard": str(row[0]),
				"query": str(row[1]),
				"page": int(row[2]),
				"observed_jobs": int(row[3]),
				"new_jobs": int(row[4]),
				"reposted_jobs": int(row[5]),
			}
			for row in rows
		]

	def recent_completed_scan_sessions(
		self,
		*,
		profile_fingerprint: str,
		limit: int = 7,
	) -> list[dict[str, Any]]:
		"""Return complete logical daily scans, including interrupted retry segments."""
		if limit < 1:
			raise ValueError("limit must be positive")
		rows = self._conn.execute(
			"""
			SELECT run_id, status, started_at, completed_at, stats_json
			FROM radar_runs
			WHERE mode = 'daily' AND status IN ('completed', 'stopped', 'failed')
			ORDER BY completed_at DESC, started_at DESC
			"""
		).fetchall()
		runs: list[dict[str, Any]] = []
		for row in rows:
			try:
				stats = json.loads(str(row[4]))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			runs.append(
				{
					"run_id": str(row[0]),
					"status": str(row[1]),
					"started_at": str(row[2]),
					"completed_at": str(row[3] or ""),
					"stats": stats,
				}
			)

		result: list[dict[str, Any]] = []
		claimed: set[str] = set()
		for index, run in enumerate(runs):
			stats = run["stats"]
			if run["run_id"] in claimed or run["status"] != "completed" or not bool(stats.get("complete")):
				continue
			logical_scan_id = str(stats.get("logical_scan_id") or "")
			if logical_scan_id:
				chain = [candidate for candidate in runs if candidate["stats"].get("logical_scan_id") == logical_scan_id]
				chain_complete = True
			else:
				chain, chain_complete = self._infer_legacy_scan_chain(runs, index)
				logical_scan_id = run["run_id"]
			chain.sort(key=lambda item: (item["started_at"], item["completed_at"]))
			run_ids = [str(item["run_id"]) for item in chain]
			claimed.update(run_ids)
			result.append(
				{
					"logical_scan_id": logical_scan_id,
					"completed_run_id": run["run_id"],
					"run_ids": run_ids,
					"segment_count": len(run_ids),
					"chain_complete": chain_complete,
					"started_at": chain[0]["started_at"],
					"completed_at": run["completed_at"],
				}
			)
			if len(result) >= limit:
				break
		return result

	@staticmethod
	def _infer_legacy_scan_chain(
		runs: list[dict[str, Any]],
		completed_index: int,
	) -> tuple[list[dict[str, Any]], bool]:
		terminal = runs[completed_index]
		chain = [terminal]
		carried = _string_set(terminal["stats"].get("carried_completed_searches"))
		terminal_day = _china_date(terminal["completed_at"] or terminal["started_at"])
		search_from = completed_index + 1
		while carried:
			matched_index = None
			for index in range(search_from, len(runs)):
				candidate = runs[index]
				if candidate["status"] == "completed":
					continue
				candidate_day = _china_date(candidate["completed_at"] or candidate["started_at"])
				if candidate_day != terminal_day:
					continue
				if _string_set(candidate["stats"].get("completed_searches")) == carried:
					matched_index = index
					break
			if matched_index is None:
				return chain, False
			predecessor = runs[matched_index]
			chain.append(predecessor)
			carried = _string_set(predecessor["stats"].get("carried_completed_searches"))
			search_from = matched_index + 1
		return chain, True

	def recent_completed_runs(
		self,
		*,
		profile_fingerprint: str,
		mode: str = "daily",
		limit: int = 7,
	) -> list[dict[str, str]]:
		if limit < 1:
			raise ValueError("limit must be positive")
		rows = self._conn.execute(
			"""
			SELECT run_id, mode, started_at, completed_at, stats_json
			FROM radar_runs
			WHERE status = 'completed' AND mode = ?
			ORDER BY completed_at DESC, started_at DESC
			""",
			(mode,),
		).fetchall()
		result: list[dict[str, str]] = []
		for row in rows:
			try:
				stats = json.loads(str(row[4]))
			except (TypeError, ValueError):
				continue
			if stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			result.append(
				{
					"run_id": str(row[0]),
					"mode": str(row[1]),
					"started_at": str(row[2]),
					"completed_at": str(row[3] or ""),
				}
			)
			if len(result) >= limit:
				break
		return result

	def profile_runs(self, *, profile_fingerprint: str, mode: str | None = None) -> list[dict[str, Any]]:
		"""Return terminal runs for one profile, newest first."""
		if mode is not None and mode not in {"baseline", "daily", "reconcile"}:
			raise ValueError(f"unsupported radar mode: {mode}")
		query = (
			"SELECT run_id, mode, status, started_at, completed_at, stats_json "
			"FROM radar_runs WHERE status IN ('completed', 'stopped', 'failed')"
		)
		params: tuple[Any, ...] = ()
		if mode is not None:
			query += " AND mode = ?"
			params = (mode,)
		query += " ORDER BY completed_at DESC, started_at DESC"
		rows = self._conn.execute(query, params).fetchall()
		result: list[dict[str, Any]] = []
		for row in rows:
			try:
				stats = json.loads(str(row[5]))
			except (TypeError, ValueError):
				continue
			if not isinstance(stats, dict) or stats.get("profile_fingerprint") != profile_fingerprint:
				continue
			result.append(
				{
					"run_id": str(row[0]),
					"mode": str(row[1]),
					"status": str(row[2]),
					"started_at": str(row[3]),
					"completed_at": str(row[4] or ""),
					"stats": stats,
				}
			)
		return result

	def integrity_metrics(self) -> dict[str, int]:
		"""Return database-wide duplicate and sensitive-field counts."""
		def scalar(query: str) -> int:
			row = self._conn.execute(query).fetchone()
			return int(row[0]) if row else 0

		return {
			"jobs": scalar("SELECT COUNT(*) FROM radar_jobs"),
			"duplicate_identity_groups": scalar(
				"SELECT COUNT(*) FROM ("
				"SELECT platform, identity_key FROM radar_jobs WHERE identity_key <> '' "
				"GROUP BY platform, identity_key HAVING COUNT(*) > 1)"
			),
			"duplicate_platform_job_id_groups": scalar(
				"SELECT COUNT(*) FROM ("
				"SELECT platform, platform_job_id FROM radar_jobs WHERE platform_job_id <> '' "
				"GROUP BY platform, platform_job_id HAVING COUNT(*) > 1)"
			),
			"security_identifiers": scalar(
				"SELECT COUNT(*) FROM radar_jobs WHERE COALESCE(security_id, '') <> '' "
				"OR payload_json LIKE '%securityId%'"
			),
			"running_runs": scalar("SELECT COUNT(*) FROM radar_runs WHERE status = 'running'"),
		}

	def record_validation_review(
		self,
		*,
		profile_fingerprint: str,
		metric: str,
		true_count: int,
		false_count: int,
		review_date: date,
		note: str = "",
	) -> dict[str, Any]:
		"""Persist one human-reviewed sample without accessing any platform data."""
		if self._read_only:
			raise PermissionError("只读 RadarStore 不能记录人工复核")
		if metric not in {"relevance", "recall", "old_newness"}:
			raise ValueError("metric must be relevance, recall, or old_newness")
		if true_count < 0 or false_count < 0 or true_count + false_count < 1:
			raise ValueError("true_count 和 false_count 必须为非负，且合计至少为 1")
		if not profile_fingerprint.strip():
			raise ValueError("profile_fingerprint 不能为空")
		created_at = utc_now().isoformat()
		cursor = self._conn.execute(
			"""
			INSERT INTO radar_validation_reviews (
				profile_fingerprint, metric, true_count, false_count, review_date, note, created_at
			) VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			(
				profile_fingerprint,
				metric,
				true_count,
				false_count,
				review_date.isoformat(),
				note.strip(),
				created_at,
			),
		)
		self._conn.commit()
		review_id = cursor.lastrowid
		if review_id is None:
			raise sqlite3.DatabaseError("人工复核记录未返回主键")
		return {
			"review_id": int(review_id),
			"metric": metric,
			"true_count": true_count,
			"false_count": false_count,
			"sample_size": true_count + false_count,
			"review_date": review_date.isoformat(),
			"platform_requests": 0,
		}

	def validation_review_summary(self, *, profile_fingerprint: str) -> dict[str, dict[str, Any]]:
		"""Return aggregate human sample counts for one immutable profile fingerprint."""
		if not self._table_exists("radar_validation_reviews"):
			return {}
		rows = self._conn.execute(
			"""
			SELECT metric, SUM(true_count), SUM(false_count), MAX(review_date)
			FROM radar_validation_reviews
			WHERE profile_fingerprint = ?
			GROUP BY metric
			""",
			(profile_fingerprint,),
		).fetchall()
		return {
			str(row[0]): {
				"true_count": int(row[1] or 0),
				"false_count": int(row[2] or 0),
				"sample_size": int(row[1] or 0) + int(row[2] or 0),
				"last_review_date": str(row[3] or ""),
			}
			for row in rows
		}

	def _table_exists(self, table_name: str) -> bool:
		row = self._conn.execute(
			"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
			(table_name,),
		).fetchone()
		return row is not None

	def finish_run(self, run_id: str, *, status: str, stats: dict[str, Any]) -> None:
		self._logical_scan_members_cache.pop(run_id, None)
		self._conn.execute(
				"UPDATE radar_runs SET status = ?, completed_at = COALESCE(completed_at, ?), stats_json = ? WHERE run_id = ?",
				(status, utc_now().isoformat(), json.dumps(stats, ensure_ascii=False, sort_keys=True), run_id),
		)
		self._conn.commit()

	def record_observations(
		self,
		run_id: str,
		mode: str,
		observations: Iterable[JobObservation],
			*,
			relevance_mode: str = "ai_pm",
	) -> list[RadarEvent]:
		relevance_mode = normalize_relevance_mode(relevance_mode)
		events: list[RadarEvent] = []
		with self._conn:
			for observation in observations:
				event = self._record_one(run_id, mode, observation, relevance_mode=relevance_mode)
				if event is not None:
					events.append(event)
		return events

	def record_page_capture(
		self,
		run_id: str,
		*,
		shard: str,
		query: str,
		page: int,
		raw_job_count: int,
		has_more: bool,
		page_size: int | None,
		same_page_state: bool,
	) -> None:
		"""Persist raw pagination facts before relevance filtering removes rows.

		This is deliberately separate from ``radar_run_observations``: that table
		tracks only AI-PM-relevant jobs, so it cannot prove whether a platform page
		was short, full, or terminal.
		"""
		if page < 1:
			raise ValueError("page must be positive")
		if raw_job_count < 0:
			raise ValueError("raw_job_count must not be negative")
		if page_size is not None and page_size < 1:
			raise ValueError("page_size must be positive when supplied")
		with self._conn:
			self._conn.execute(
				"""
				INSERT INTO radar_page_captures (
					run_id, shard, query, page, raw_job_count, has_more,
					page_size, same_page_state, captured_at
				)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(run_id, shard, query, page) DO UPDATE SET
					raw_job_count = excluded.raw_job_count,
					has_more = excluded.has_more,
					page_size = excluded.page_size,
					same_page_state = excluded.same_page_state,
					captured_at = excluded.captured_at
				""",
				(
					run_id,
					shard,
					query,
					page,
					raw_job_count,
					int(has_more),
					page_size,
					int(same_page_state),
					utc_now().isoformat(),
				),
			)

	def _record_one(
			self,
			run_id: str,
			mode: str,
			observation: JobObservation,
			*,
			relevance_mode: str = "ai_pm",
	) -> RadarEvent | None:
		now = observation.observed_at.isoformat()
		existing = self._conn.execute(
			"SELECT id, content_hash, title, payload_json FROM radar_jobs "
			"WHERE platform = ? AND identity_key = ?",
			(observation.platform, observation.identity_key),
		).fetchone()
		if existing is not None:
			job_pk, previous_content_hash = int(existing[0]), str(existing[1])
			previous_relevant = _stored_job_is_relevant(
					str(existing[2]), str(existing[3]), relevance_mode=relevance_mode,
			)
			if not previous_relevant and is_relevant(observation, relevance_mode=relevance_mode):
					# A row collected under another relevance policy is not prior evidence
					# that this job was already relevant for this profile.
				event_type = "baseline" if mode == "baseline" else "new"
			else:
				event_type = "updated" if previous_content_hash != observation.content_hash else ""
			self._conn.execute(
				"""
				UPDATE radar_jobs
				SET security_id = ?, title = ?, company = ?, city = ?, district = ?, salary = ?,
					experience = ?, education = ?, content_hash = ?, published_at = COALESCE(?, published_at),
					last_seen_at = ?, last_run_id = ?, payload_json = ?
				WHERE id = ?
				""",
				(
					"",
					observation.title,
					observation.company,
					observation.city,
					observation.district,
					observation.salary,
					observation.experience,
					observation.education,
					observation.content_hash,
					observation.published_at.isoformat() if observation.published_at else None,
					now,
					run_id,
									observation_json(observation, relevance_mode=relevance_mode),
					job_pk,
				),
			)
			self._upsert_source(job_pk, observation, now)
			self._upsert_run_observation(run_id, job_pk, observation, now)
			if event_type:
				self._insert_event(run_id, job_pk, event_type, "", now)
				return RadarEvent(event_type=event_type, job=observation)
			return None

		previous_job_id = self._find_previous_job_id(
				observation, current_run_id=run_id, relevance_mode=relevance_mode,
		)
		cursor = self._conn.execute(
			"""
			INSERT INTO radar_jobs (
				platform, identity_key, platform_job_id, family_key, security_id,
				title, company, city, district, salary, experience, education,
				content_hash, published_at, first_seen_at, last_seen_at,
				first_run_id, last_run_id, payload_json
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				observation.platform,
				observation.identity_key,
				observation.platform_job_id,
				observation.family_key,
				"",
				observation.title,
				observation.company,
				observation.city,
				observation.district,
				observation.salary,
				observation.experience,
				observation.education,
				observation.content_hash,
				observation.published_at.isoformat() if observation.published_at else None,
				now,
				now,
				run_id,
				run_id,
							observation_json(observation, relevance_mode=relevance_mode),
			),
		)
		if cursor.lastrowid is None:
			raise RuntimeError("failed to create radar job record")
		job_pk = int(cursor.lastrowid)
		self._upsert_source(job_pk, observation, now)
		self._upsert_run_observation(run_id, job_pk, observation, now)
		event_type = "baseline" if mode == "baseline" else ("reposted" if previous_job_id else "new")
		self._insert_event(run_id, job_pk, event_type, previous_job_id, now)
		return RadarEvent(event_type=event_type, job=observation, previous_job_id=previous_job_id)

	def _find_previous_job_id(
			self,
			observation: JobObservation,
			*,
			current_run_id: str,
			relevance_mode: str = "ai_pm",
	) -> str:
		# A retry creates a new physical run id while retaining its logical scan
		# id. Listings first discovered in an earlier segment of that same scan are
		# contemporaneous observations, not historical repost candidates.
		excluded_first_run_ids = self._logical_scan_run_ids(current_run_id)
		excluded_run_ids = tuple(sorted(excluded_first_run_ids))
		excluded_marks = ",".join("?" for _ in excluded_run_ids)
		exact_candidates = self._conn.execute(
			f"""
			SELECT platform_job_id, title, company, district, salary, experience, education,
				payload_json, first_run_id
			FROM radar_jobs
			WHERE platform = ? AND family_key = ? AND first_run_id NOT IN ({excluded_marks})
			ORDER BY last_seen_at DESC LIMIT 20
			""",
			(observation.platform, observation.family_key, *excluded_run_ids),
		).fetchall()
		for exact in exact_candidates:
			if str(exact[8]) in excluded_first_run_ids:
				continue
			if _stored_job_is_relevant(
					str(exact[1]), str(exact[7]), relevance_mode=relevance_mode,
			) and _looks_like_repost(
				observation,
				exact,
				include_district=False,
			):
				return str(exact[0])

		candidates = self._conn.execute(
			f"""
			SELECT platform_job_id, title, company, district, salary, experience, education,
				payload_json, first_run_id
			FROM radar_jobs
			WHERE platform = ? AND city = ? AND family_key <> ?
				AND first_run_id NOT IN ({excluded_marks})
			ORDER BY last_seen_at DESC LIMIT 200
			""",
			(observation.platform, observation.city, observation.family_key, *excluded_run_ids),
		).fetchall()
		for candidate in candidates:
			if str(candidate[8]) in excluded_first_run_ids:
				continue
			if not _stored_job_is_relevant(
					str(candidate[1]), str(candidate[7]), relevance_mode=relevance_mode,
			):
				continue
			if _looks_like_repost(observation, candidate):
				return str(candidate[0])
		return ""

	def _logical_scan_run_ids(self, run_id: str) -> set[str]:
		"""Return physical run ids belonging to ``run_id``'s logical scan.

		Older direct-store callers and historical rows may not have a logical scan
		id yet; in that case the conservative legacy scope is the current run.
		"""
		cached = self._logical_scan_members_cache.get(run_id)
		if cached is not None:
			return set(cached[1])
		row = self._conn.execute(
			"SELECT stats_json FROM radar_runs WHERE run_id = ?",
			(run_id,),
		).fetchone()
		if row is None:
			return {run_id}
		try:
			current_stats = json.loads(str(row[0]))
		except (TypeError, ValueError):
			return {run_id}
		if not isinstance(current_stats, dict):
			return {run_id}
		logical_scan_id = str(current_stats.get("logical_scan_id") or "")
		if not logical_scan_id:
			legacy_members = frozenset({run_id})
			self._logical_scan_members_cache[run_id] = ("", legacy_members)
			return set(legacy_members)

		members: set[str] = {run_id}
		for candidate_run_id, raw_stats in self._conn.execute(
			"SELECT run_id, stats_json FROM radar_runs"
		):
			try:
				candidate_stats = json.loads(str(raw_stats))
			except (TypeError, ValueError):
				continue
			if not isinstance(candidate_stats, dict):
				continue
			candidate_logical_id = str(candidate_stats.get("logical_scan_id") or candidate_run_id)
			if candidate_logical_id == logical_scan_id:
				members.add(str(candidate_run_id))
		frozen_members = frozenset(members)
		self._logical_scan_members_cache[run_id] = (logical_scan_id, frozen_members)
		return set(frozen_members)

	def _invalidate_logical_scan_members_cache(self, run_id: str, stats: dict[str, Any]) -> None:
		"""Invalidate only when a checkpoint changes this run's logical identity."""
		cached = self._logical_scan_members_cache.get(run_id)
		if cached is None:
			return
		logical_scan_id = str(stats.get("logical_scan_id") or "")
		if cached[0] != logical_scan_id:
			self._logical_scan_members_cache.pop(run_id, None)

	def _upsert_source(self, job_pk: int, observation: JobObservation, now: str) -> None:
		self._conn.execute(
			"""
			INSERT INTO radar_job_sources (
				job_pk, query, shard, first_seen_at, last_seen_at,
				first_page, last_page, first_rank, last_rank
			)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(job_pk, query, shard) DO UPDATE SET
				last_seen_at = excluded.last_seen_at,
				first_page = CASE
					WHEN radar_job_sources.first_page = 0 THEN excluded.first_page
					ELSE radar_job_sources.first_page
				END,
				last_page = excluded.last_page,
				first_rank = CASE
					WHEN radar_job_sources.first_rank = 0 THEN excluded.first_rank
					ELSE radar_job_sources.first_rank
				END,
				last_rank = excluded.last_rank
			""",
			(
				job_pk,
				observation.query,
				observation.shard,
				now,
				now,
				observation.source_page,
				observation.source_page,
				observation.source_rank,
				observation.source_rank,
			),
		)

	def _upsert_run_observation(
		self,
		run_id: str,
		job_pk: int,
		observation: JobObservation,
		now: str,
	) -> None:
		self._conn.execute(
			"""
			INSERT INTO radar_run_observations (
				run_id, job_pk, query, shard, page, rank, observed_at
			)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(run_id, job_pk, query, shard) DO UPDATE SET
				page = CASE
					WHEN radar_run_observations.page = 0 THEN excluded.page
					WHEN excluded.page = 0 THEN radar_run_observations.page
					ELSE MIN(radar_run_observations.page, excluded.page)
				END,
				rank = CASE
					WHEN radar_run_observations.rank = 0 THEN excluded.rank
					WHEN excluded.rank = 0 THEN radar_run_observations.rank
					ELSE MIN(radar_run_observations.rank, excluded.rank)
				END
			""",
			(
				run_id,
				job_pk,
				observation.query,
				observation.shard,
				observation.source_page,
				observation.source_rank,
				now,
			),
		)

	def _insert_event(
		self,
		run_id: str,
		job_pk: int,
		event_type: str,
		previous_job_id: str,
		created_at: str,
	) -> None:
		self._conn.execute(
			"""
			INSERT OR IGNORE INTO radar_events (run_id, job_pk, event_type, previous_job_id, created_at)
			VALUES (?, ?, ?, ?, ?)
			""",
			(run_id, job_pk, event_type, previous_job_id, created_at),
		)

	def run_summary(self, run_id: str) -> dict[str, Any]:
		run = self._conn.execute(
			"SELECT mode, status, started_at, completed_at, stats_json FROM radar_runs WHERE run_id = ?",
			(run_id,),
		).fetchone()
		if run is None:
			raise KeyError(run_id)
		counts = {
			str(row[0]): int(row[1])
			for row in self._conn.execute(
				"SELECT event_type, COUNT(*) FROM radar_events WHERE run_id = ? GROUP BY event_type",
				(run_id,),
			)
		}
		return {
			"run_id": run_id,
			"mode": run[0],
			"status": run[1],
			"started_at": run[2],
			"completed_at": run[3],
			"stats": json.loads(run[4]),
			"event_counts": counts,
		}

	def close(self) -> None:
		self._conn.close()

	def __enter__(self) -> RadarStore:
		return self

	def __exit__(self, *_: Any) -> None:
		self.close()


def _string_set(value: Any) -> set[str]:
	if not isinstance(value, list):
		return set()
	return {str(item) for item in value if isinstance(item, str)}


def _unverified_page_capture_completion(stats: dict[str, Any]) -> bool:
	"""Reject only explicit browser captures that did not prove their endpoint.

	Legacy native records retain their historic completion semantics. An explicit
	``page_capture`` run, however, must carry an empty terminal-evidence list;
	missing metadata is not evidence that a visible first page was terminal.
	"""
	if stats.get("boss_search_transport") != "page_capture":
		return False
	missing = stats.get("terminal_evidence_missing_searches")
	return not isinstance(missing, list) or any(
		isinstance(item, str) and item.strip() for item in missing
	)


def _positive_int(value: Any) -> int | None:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return None
	parsed = int(value)
	return parsed if parsed > 0 else None


def _safe_budget_checkpoint(stats: dict[str, Any]) -> bool:
  """Whether an interrupted segment has an auditable, safe resume point.

  Usually this is a pure request-budget boundary. A page-visibility timeout is
  also safe only when the extension refused before any BOSS load-more action and
  the scanner saved one exact next-page checkpoint. Authentication, risk,
  navigation, and ambiguous browser failures remain non-resumable.  One
  separately bounded first-page schema state is also resumable: the extension
  stopped before a loader existed, saved no page, and the exact same window can
  safely start from page one on the next physical segment.
  """
  ordinary_budget = bool(
          not stats.get("complete")
          and not stats.get("stop_code")
          and not stats.get("stop_reason")
          and not stats.get("operator_handoff")
          and not stats.get("failed_searches")
          and _positive_int(stats.get("requests")) is not None
          and (stats.get("search_progress") or stats.get("truncated_searches"))
  )
  if ordinary_budget:
          return True
  reason = stats.get("stop_reason")
  failed = stats.get("failed_searches")
  progress = stats.get("search_progress")
  schema_wait_reason = (
          "Bridge page capture 失败: Visible BOSS page is not a settled result page: "
          "schema-invalid: hasMore(undefined)"
  )
  if (
          not stats.get("complete")
          and not stats.get("operator_handoff")
          and stats.get("stop_code") == "BROWSER_ERROR"
          and reason == schema_wait_reason
          and isinstance(failed, list)
          and len(failed) == 1
          and isinstance(progress, dict)
          and not progress
          and _positive_int(stats.get("requests")) is not None
  ):
          key = failed[0].split(":", 1)[0] if isinstance(failed[0], str) else ""
          return bool(key and failed[0] == f"{key}: BROWSER_ERROR {reason}")
  if (
          stats.get("complete")
          or stats.get("operator_handoff")
          or stats.get("stop_code") != "PAGE_VISIBILITY_TIMEOUT"
          or not isinstance(reason, str)
          or not reason.startswith("页面持续不可见超过 ")
          or not reason.endswith("；未发送加载动作")
          or not isinstance(failed, list)
          or len(failed) != 1
          or not isinstance(progress, dict)
          or len(progress) != 1
  ):
          return False
  key, checkpoint = next(iter(progress.items()))
  if not isinstance(key, str) or not isinstance(checkpoint, dict):
          return False
  next_page = _positive_int(checkpoint.get("next_page"))
  source_rank = _nonnegative_int(checkpoint.get("source_rank"))
  return bool(
          next_page is not None
          and next_page >= 2
          and source_rank is not None
          and failed[0] == f"{key}: PAGE_VISIBILITY_TIMEOUT {reason}"
  )


def _nonnegative_int(value: Any) -> int | None:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return None
	parsed = int(value)
	return parsed if parsed >= 0 else None


def _china_date(value: str) -> date | None:
	try:
		return datetime.fromisoformat(value).astimezone(_CHINA_TZ).date()
	except (TypeError, ValueError):
		return None


def _looks_like_repost(
	observation: JobObservation,
	candidate: tuple[Any, ...],
	*,
	include_district: bool = True,
) -> bool:
	_, old_title, old_company, old_district, old_salary, old_experience, old_education, *_ = candidate
	if normalize_text(observation.company) != normalize_text(old_company):
		return False

	new_title = normalize_text(observation.title)
	previous_title = normalize_text(old_title)
	if not new_title or not previous_title:
		return False
	if new_title == previous_title:
		required_feature_matches = 1
	else:
		ratio = SequenceMatcher(None, new_title, previous_title).ratio()
		shorter, longer = sorted((new_title, previous_title), key=len)
		contained = len(shorter) >= 6 and shorter in longer and len(shorter) / len(longer) >= 0.65
		if ratio < 0.82 and not contained:
			return False
		required_feature_matches = 2
	if _is_anonymized_company(observation.company):
		required_feature_matches += 1

	feature_pairs = [
		(observation.salary, old_salary),
		(observation.experience, old_experience),
		(observation.education, old_education),
	]
	if include_district:
		feature_pairs.insert(0, (observation.district, old_district))
	matches = sum(
		bool(new_value) and bool(old_value) and normalize_text(new_value) == normalize_text(old_value)
		for new_value, old_value in feature_pairs
	)
	return matches >= required_feature_matches


def _is_anonymized_company(value: str) -> bool:
	return bool(re.search(r"某(?:大型|中型|小型|知名|头部|互联网|科技|公司|企业)", value))


def _stored_ai_evidence(payload_json: str) -> tuple[str, ...]:
	try:
		payload = json.loads(payload_json)
	except (TypeError, ValueError):
		return ()
	if not isinstance(payload, dict):
		return ()
	values = payload.get("ai_evidence")
	if not isinstance(values, list):
		return ()
	return tuple(str(value).strip() for value in values if str(value).strip())


def _stored_relevance_mode(payload_json: str) -> str:
	try:
		payload = json.loads(payload_json)
	except (TypeError, ValueError):
		return "ai_pm"
	if not isinstance(payload, dict):
		return "ai_pm"
	try:
		return normalize_relevance_mode(str(payload.get("relevance_mode") or "ai_pm"))
	except ValueError:
		return "ai_pm"


def _resolve_stored_relevance_mode(
	payload_jsons: Iterable[str], *, requested: str | None
) -> str:
	"""Use one persisted mode, never silently treating an adjacent run as AI PM."""
	if requested is not None:
		return normalize_relevance_mode(requested)
	modes = {_stored_relevance_mode(payload_json) for payload_json in payload_jsons}
	if not modes:
		return "ai_pm"
	if len(modes) != 1:
		raise ValueError("混合 relevance_mode 的岗位读取必须显式指定 relevance_mode")
	return modes.pop()


def _stored_relevance_evidence(payload_json: str) -> tuple[str, ...]:
	try:
		payload = json.loads(payload_json)
	except (TypeError, ValueError):
		return _stored_ai_evidence(payload_json)
	if not isinstance(payload, dict):
		return _stored_ai_evidence(payload_json)
	values = payload.get("relevance_evidence")
	if not isinstance(values, list):
		return _stored_ai_evidence(payload_json)
	return tuple(str(value).strip() for value in values if str(value).strip())


def _stored_job_is_relevant(
    title: str,
    payload_json: str,
    *,
    relevance_mode: str = "ai_pm",
) -> bool:
	mode = normalize_relevance_mode(relevance_mode)
	if _stored_relevance_mode(payload_json) != mode:
		return False
	return is_relevant(
		JobObservation(
			platform="zhipin",
			query="",
			shard="",
			title=title,
			company="",
			city="",
                        description=" ".join(_stored_relevance_evidence(payload_json)),
		),
		relevance_mode=mode,
	)
