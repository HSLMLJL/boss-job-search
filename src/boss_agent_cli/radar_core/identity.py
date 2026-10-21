"""Deterministic listing identities with no dependency on a job platform."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from typing import Any


def normalize_text(value: Any) -> str:
	"""Normalize text for identity comparison without rewriting display values."""
	text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
	return "".join(char for char in text if char.isalnum())


def fallback_identity_key(
	*,
	source: Any,
	company: Any,
	title: Any,
	city: Any,
	district: Any,
	experience: Any,
	education: Any,
) -> str:
	"""Build the conservative identity used when a source lacks a stable ID."""
	return "fp:" + _stable_hash(
		[
			normalize_text(source),
			normalize_text(company),
			normalize_text(title),
			normalize_text(city),
			normalize_text(district),
			normalize_text(experience),
			normalize_text(education),
		]
	)


def family_fingerprint(*, source: Any, company: Any, title: Any, city: Any, district: Any) -> str:
	"""Build a deliberately broad candidate key for possible repost detection."""
	return _stable_hash(
		[
			normalize_text(source),
			normalize_text(company),
			normalize_text(title),
			normalize_text(city),
			normalize_text(district),
		]
	)


def content_fingerprint(
	*,
	title: Any,
	company: Any,
	city: Any,
	district: Any,
	salary: Any,
	experience: Any,
	education: Any,
	skills: Iterable[Any],
	welfare: Iterable[Any],
	labels: Iterable[Any],
	description: Any,
) -> str:
	"""Fingerprint meaningful listing content while ignoring metadata ordering."""
	return _stable_hash(
		[
			normalize_text(title),
			normalize_text(company),
			normalize_text(city),
			normalize_text(district),
			normalize_text(salary),
			normalize_text(experience),
			normalize_text(education),
			_normalized_values(skills),
			_normalized_values(welfare),
			_normalized_values(labels),
			normalize_text(description),
		]
	)


def _normalized_values(values: Iterable[Any]) -> str:
	return " ".join(sorted({normalize_text(value) for value in values if normalize_text(value)}))


def _stable_hash(parts: Iterable[str]) -> str:
	raw = "\x1f".join(parts).encode("utf-8")
	return hashlib.sha256(raw).hexdigest()
