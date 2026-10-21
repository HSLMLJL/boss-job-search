"""Automation event helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from boss_agent_cli.automation.models import (
	AutomationEvent,
	EventStatus,
	PlatformAction,
)


def now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


def stable_action_id(
	platform: str,
	candidate_key: str,
	action: PlatformAction | str,
	ts: str,
) -> str:
	raw = f"{platform}|{candidate_key}|{action}|{ts}"
	# Compatibility identifier only; it is not a password hash, signature, or
	# integrity primitive, so preserve the existing deterministic SHA-1 output.
	return sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def make_event(
	platform: str,
	candidate_key: str,
	action: PlatformAction,
	status: EventStatus,
	confidence: float,
	reason: str,
	result: dict[str, Any] | None = None,
) -> AutomationEvent:
	return AutomationEvent(
		ts=now_iso(),
		platform=platform,
		role="recruiter",
		candidate_key=candidate_key,
		action=action.value,
		status=status.value,
		confidence=round(confidence, 4),
		reason=reason,
		result=result or {},
	)
