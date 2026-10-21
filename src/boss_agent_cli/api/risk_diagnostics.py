"""Safe, minimal diagnostics for BOSS risk responses.

The browser/API layers may inspect a complete response transiently, but callers
must never retain or export that response: it can include cookies, challenges,
or one-time verification credentials. This module deliberately returns only
the small scalar fields an operator can act on.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit


_URL_KEYS = ("verifyUrl", "verify_url", "redirectUrl", "redirect_url", "url")
_SAFE_VERIFY_PATHS = {"/verify", "/verify.html", "/web/passport/zp/verify.html"}
_SENSITIVE_MESSAGE_MARKERS = ("http://", "https://", "token", "cookie", "seed", "session", "challenge")


def extract_risk_diagnostics(raw: Mapping[str, Any]) -> dict[str, int | bool | str | None]:
	"""Extract only safe operator diagnostics from one transient response.

	No reference to ``raw`` is returned. A verification URL is retained only when
	the *original* URL is a strict BOSS verification URL without credentials or
	unknown query data; unsafe candidates only set the redaction flag.
	"""
	platform_code = _safe_platform_code(raw.get("code"))
	verification_url = None
	verification_url_redacted = False
	for candidate in _candidate_verification_urls(raw):
		safe_url = _safe_verification_url(candidate)
		if safe_url is not None:
			verification_url = safe_url
			break
		verification_url_redacted = True
	return {
		"platform_code": platform_code,
		"verification_url_available": verification_url is not None,
		"verification_url": verification_url,
		"verification_url_redacted": verification_url_redacted,
		"needs_browser_handoff": platform_code in {36, 37},
	}


def safe_platform_message(raw: Mapping[str, Any], default: str) -> str:
	"""Return a bounded non-secret platform message, never a serialized data body."""
	for key in ("message", "msg", "error"):
		value = raw.get(key)
		if not isinstance(value, str):
			continue
		message = value.strip()
		if (
			0 < len(message) <= 240
			and not _contains_control_characters(message)
			and not any(marker in message.lower() for marker in _SENSITIVE_MESSAGE_MARKERS)
		):
			return message
	return default


def _safe_platform_code(value: Any) -> int | None:
	if isinstance(value, int) and not isinstance(value, bool):
		return value if -999_999 <= value <= 999_999 else None
	if isinstance(value, str) and 0 < len(value) <= 6 and value.isascii() and value.isdecimal():
		return int(value)
	return None


def _candidate_verification_urls(raw: Mapping[str, Any]) -> list[str]:
	candidates: list[str] = []
	for container in (raw, raw.get("zpData")):
		if not isinstance(container, Mapping):
			continue
		for key in _URL_KEYS:
			value = container.get(key)
			if isinstance(value, str) and value:
				candidates.append(value)
	return candidates


def _safe_verification_url(value: str) -> str | None:
	if _contains_control_characters(value):
		return None
	try:
		parsed = urlsplit(value)
		port = parsed.port
	except ValueError:
		return None
	host = (parsed.hostname or "").rstrip(".").lower()
	if (
		parsed.scheme != "https"
		or not host
		or (host != "zhipin.com" and not host.endswith(".zhipin.com"))
		or parsed.username is not None
		or parsed.password is not None
		or port not in (None, 443)
		or parsed.fragment
	):
		return None
	try:
		query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
	except ValueError:
		return None
	if any(key != "code" or query_value not in {"36", "37"} for key, query_value in query_items):
		return None
	if len(query_items) > 1 or parsed.path.lower() not in _SAFE_VERIFY_PATHS:
		return None
	return value


def _contains_control_characters(value: str) -> bool:
	return any(ord(character) < 32 or ord(character) == 127 for character in value)
