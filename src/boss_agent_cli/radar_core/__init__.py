"""Platform-neutral primitives for a job-radar implementation.

This package intentionally contains no browser, account, city-code, URL, or
source-adapter logic. It is the part that can later be extracted into an
independent open-source project with mock or authorized data sources.
"""

from boss_agent_cli.radar_core.identity import (
	content_fingerprint,
	fallback_identity_key,
	family_fingerprint,
	normalize_text,
)

__all__ = [
	"content_fingerprint",
	"fallback_identity_key",
	"family_fingerprint",
	"normalize_text",
]
