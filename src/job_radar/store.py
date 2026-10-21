"""Local task checkpoints and job history, independent from the legacy radar DB."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

STATUSES = ("待看", "考虑", "已投", "面试", "不合适")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class JobStore:
    def __init__(self, data_dir: Path | str):
        self.root = Path(data_dir).expanduser() / "job-radar"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "workspace.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_key TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT '待看', notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS run_jobs (
                    run_id TEXT NOT NULL, job_key TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (run_id, job_key)
                );
                CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_run(self, config: dict, demo: bool = False) -> dict:
        moment = now()
        data = {"id": uuid.uuid4().hex, "config": config, "demo": demo, "status": "queued",
                "counts": {}, "checkpoint": {}, "created_at": moment, "updated_at": moment, "message": "等待开始"}
        with self._connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?)", (data["id"], _json(data)))
        return data

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_run(self, run_id: str, **updates: Any) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            data = json.loads(row[0])
            data.update({k: v for k, v in updates.items() if k not in {"id", "created_at"}})
            data["updated_at"] = now()
            db.execute("UPDATE runs SET data=? WHERE id=?", (_json(data), run_id))
        return data

    def upsert_job(self, run_id: str, job: dict) -> dict:
        data = dict(job)
        key = str(data.get("job_key", ""))
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", key):
            raise ValueError("Invalid job key")
        moment = now()
        # Descriptions are deliberately excluded: opting into JD must not invent a listing change.
        fingerprint = hashlib.sha256(_json({k: data.get(k) for k in
            ("title", "company", "city", "district", "salary", "experience", "education", "job_type")}).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
            same_run = db.execute("SELECT data FROM run_jobs WHERE run_id=? AND job_key=?", (run_id, key)).fetchone()
            change = "first_seen" if previous is None else ("seen" if previous["fingerprint"] == fingerprint else "changed")
            if same_run:
                saved = json.loads(same_run[0])
                change = saved.get("change", change)
                data["queries"] = list(dict.fromkeys(saved.get("queries", []) + data.get("queries", [])))
            data.update(first_seen=previous["first_seen"] if previous else moment, last_seen=moment, change=change,
                        status=previous["status"] if previous else "待看", notes=previous["notes"] if previous else "")
            db.execute("""INSERT INTO jobs (job_key,first_seen,last_seen,fingerprint) VALUES(?,?,?,?)
                          ON CONFLICT(job_key) DO UPDATE SET last_seen=excluded.last_seen,fingerprint=excluded.fingerprint""",
                       (key, data["first_seen"], moment, fingerprint))
            db.execute("INSERT OR REPLACE INTO run_jobs VALUES(?,?,?)", (run_id, key, _json(data)))
        return data

    def get_jobs(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT r.data,j.status,j.notes FROM run_jobs r JOIN jobs j USING(job_key)
                                 WHERE r.run_id=? ORDER BY r.rowid""", (run_id,)).fetchall()
        return [{**json.loads(row["data"]), "status": row["status"], "notes": row["notes"]} for row in rows]

    def get_job(self, run_id: str, job_key: str) -> dict | None:
        return next((job for job in self.get_jobs(run_id) if job["job_key"] == job_key), None)

    def update_job_status(self, job_key: str, status: str, notes: str = "") -> None:
        if status not in STATUSES or not isinstance(notes, str) or len(notes) > 5000:
            raise ValueError("请选择有效进度，备注不超过 5000 字")
        with self._connect() as db:
            if db.execute("UPDATE jobs SET status=?,notes=? WHERE job_key=?", (status, notes, job_key)).rowcount != 1:
                raise KeyError(job_key)

    def save_template(self, config: dict, template_id: str | None = None) -> dict:
        template_id = template_id or uuid.uuid4().hex
        data = {"id": template_id, "name": config["name"], "config": config, "updated_at": now()}
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO templates VALUES(?,?)", (template_id, _json(data)))
        return data

    def list_templates(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT data FROM templates ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]
