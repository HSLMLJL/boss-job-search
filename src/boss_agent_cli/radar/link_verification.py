"""Conservative, offline classification for explicitly captured BOSS job pages.

This module deliberately does not fetch pages.  A caller supplies a normalized
capture from a visible page, and only narrow, page-header evidence is accepted.
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


_JOB_URL = re.compile(r"^/job_detail/([A-Za-z0-9_-]+)\.html$")
_CLOSED = ("职位已关闭", "职位已下架")
_RECRUITING = "招聘中"
_COMMUNICATE = "立即沟通"


class LinkVerificationError(ValueError):
    """A manifest, URL, capture, or append-only journal is invalid."""


def boss_stable_job_id(url: str) -> str:
    """Return the stable BOSS job-detail identity, rejecting unsafe URLs."""
    if not isinstance(url, str) or len(url) > 500 or any(ord(char) < 32 for char in url):
        raise LinkVerificationError("job URL must be safe text")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.zhipin.com", "zhipin.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LinkVerificationError("URL must be a canonical HTTPS BOSS job-detail URL")
    match = _JOB_URL.fullmatch(parsed.path)
    if not match:
        raise LinkVerificationError("URL must identify one BOSS job_detail stable ID")
    return match.group(1)


def classify_capture(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one normalized capture as communicable, closed, or unknown.

    Errors, login/risk gates, non-identical detail URLs, and contradictory page
    signals are deliberately ``unknown`` and carry ``stop_batch=True`` when a caller
    should stop the verification run.
    """
    if not isinstance(capture, Mapping):
        raise LinkVerificationError("capture must be a mapping")
    requested = _required_url(capture, "requested_url")
    actual = capture.get("actual_url")
    if not isinstance(actual, str):
        actual = ""
    requested_id = boss_stable_job_id(requested)
    base = {"requested_url": requested, "actual_url": actual, "stable_job_id": requested_id}

    if _truthy(capture.get("risk_text")):
        return {**base, "status": "unknown", "evidence": "risk_gate", "error_short": _short(capture.get("risk_text")), "stop_batch": True, "checked_at": _now()}
    if _truthy(capture.get("login_required")):
        return {**base, "status": "unknown", "evidence": "login_required", "stop_batch": True, "checked_at": _now()}
    if _truthy(capture.get("error")):
        return {**base, "status": "unknown", "evidence": "browser_read_error", "error_short": _short(capture.get("error")), "stop_batch": True, "checked_at": _now()}
    try:
        actual_id = boss_stable_job_id(actual)
    except LinkVerificationError:
        return {**base, "status": "unknown", "evidence": "actual_url_not_requested_detail", "stop_batch": True, "checked_at": _now()}
    if requested_id != actual_id:
        return {**base, "status": "unknown", "evidence": "detail_identity_mismatch", "stop_batch": True, "checked_at": _now()}

    header = _text(capture.get("header_text"))
    title = _text(capture.get("job_title"))
    if not header or not title:
        return {**base, "status": "unknown", "evidence": "empty_job_header", "stop_batch": True, "checked_at": _now()}
    expected_title = _text(capture.get("expected_title"))
    if expected_title and _normalized(title) != _normalized(expected_title):
        return {**base, "status": "unknown", "evidence": "expected_title_mismatch", "stop_batch": False, "checked_at": _now()}
    closed = any(marker in header for marker in _CLOSED)
    title_in_header = bool(title) and _normalized(title) in _normalized(header)
    communicable = (
        title_in_header
        and _RECRUITING in header
        and capture.get("communicate_visible") is True
        and capture.get("communicate_enabled") is True
        and _COMMUNICATE in header
    )
    if closed and not title_in_header:
        return {**base, "status": "unknown", "evidence": "closed_without_matching_title", "stop_batch": False, "checked_at": _now()}
    if closed and communicable:
        return {**base, "status": "unknown", "evidence": "contradictory_header", "stop_batch": False, "checked_at": _now()}
    if closed:
        return {**base, "status": "closed", "evidence": "explicit_closed_header", "stop_batch": False, "checked_at": _now()}
    if communicable:
        return {**base, "status": "communicable", "evidence": "matching_recruiting_enabled_communicate", "stop_batch": False, "checked_at": _now()}
    return {**base, "status": "unknown", "evidence": "insufficient_header_evidence", "stop_batch": False, "checked_at": _now()}


def load_manifest(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    """Validate and return jobs in manifest order (``stable_job_id``, ``url``)."""
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("jobs"), list):
        raise LinkVerificationError("manifest.jobs must be a list")
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in manifest["jobs"]:
        if not isinstance(item, Mapping):
            raise LinkVerificationError("manifest jobs must be mappings")
        stable_id, url = item.get("stable_job_id"), item.get("url")
        if not isinstance(stable_id, str) or not stable_id or not isinstance(url, str):
            raise LinkVerificationError("each job requires stable_job_id and url")
        if stable_id != boss_stable_job_id(url):
            raise LinkVerificationError("manifest stable_job_id does not match URL identity")
        if stable_id in seen:
            raise LinkVerificationError("duplicate stable_job_id in manifest")
        seen.add(stable_id)
        jobs.append({"stable_job_id": stable_id, "url": url})
    return jobs


class LinkVerificationJournal:
    """A resumable append-only JSONL journal keyed by stable job ID."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @property
    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LinkVerificationError(f"cannot read journal: {exc}") from exc
        for number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LinkVerificationError(f"invalid JSONL journal line {number}") from exc
            if not isinstance(record, dict) or not isinstance(record.get("stable_job_id"), str):
                raise LinkVerificationError(f"invalid journal record at line {number}")
            identifier = record["stable_job_id"]
            if identifier in seen:
                raise LinkVerificationError("journal contains duplicate stable_job_id")
            try:
                requested_id = boss_stable_job_id(record.get("requested_url"))
            except LinkVerificationError as exc:
                raise LinkVerificationError(f"invalid journal record at line {number}") from exc
            if (
                identifier != requested_id
                or not isinstance(record.get("actual_url"), str)
                or record.get("status") not in {"communicable", "closed", "unknown"}
                or not isinstance(record.get("evidence"), str)
                or not record["evidence"].strip()
                or not isinstance(record.get("stop_batch"), bool)
                or not _aware(record.get("checked_at"))
                or not _aware(record.get("verified_at"))
            ):
                raise LinkVerificationError(f"invalid journal record at line {number}")
            seen.add(identifier)
            records.append(record)
        return records

    def pending(self, manifest: Mapping[str, Any]) -> list[dict[str, str]]:
        completed = {record["stable_job_id"] for record in self.records}
        return [job for job in load_manifest(manifest) if job["stable_job_id"] not in completed]

    def append(self, capture: Mapping[str, Any]) -> dict[str, Any]:
        result = classify_capture(capture)
        if result["stable_job_id"] in {record["stable_job_id"] for record in self.records}:
            raise LinkVerificationError("refusing duplicate stable_job_id journal entry")
        record = {**result, "verified_at": _now()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise LinkVerificationError(f"cannot append journal: {exc}") from exc
        return record

    def export_csv(self, path: Path | str) -> Path:
        output = Path(path)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite CSV: {output}")
        rows = self.records
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["stable_job_id", "status", "evidence", "stop_batch", "requested_url", "actual_url", "checked_at", "verified_at"])
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in writer.fieldnames} for row in rows)
        return output


def _required_url(capture: Mapping[str, Any], name: str) -> str:
    value = capture.get(name)
    if not isinstance(value, str) or not value:
        raise LinkVerificationError(f"capture.{name} is required")
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized(value: str) -> str:
    """Normalize only presentation variation, never infer a different title."""
    return " ".join(value.split()).casefold()


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and bool(value.strip()))


def _short(value: Any) -> str:
    return " ".join(str(value).split())[:500]


def _aware(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _now() -> str:
    """An ISO-8601 timestamp that is always explicitly timezone-aware."""
    return datetime.now(timezone.utc).isoformat()
