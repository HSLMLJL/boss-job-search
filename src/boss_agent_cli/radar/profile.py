from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from boss_agent_cli.radar.models import RELEVANCE_MODES, normalize_relevance_mode


_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TIER_NAME_PATTERN = re.compile(r"^[\w-]{1,40}$")
_CITY_CODE_PATTERN = re.compile(r"^\d{9}$")
_MAX_CITIES = 50
_BOSS_SEARCH_TRANSPORTS = frozenset({"native_joblist", "page_capture"})
_BOSS_PAGE_LAYOUTS = frozenset({"current_city", "legacy_spa"})


class RadarProfileError(ValueError):
	"""Raised when a radar profile is missing or invalid."""


@dataclass(frozen=True)
class RadarCity:
	name: str
	code: str
	tier: str = ""
	max_pages_per_query: int | None = None


@dataclass(frozen=True)
class RadarCityTier:
	name: str
	max_pages_per_query: int


@dataclass(frozen=True)
class RadarJobExclusions:
	"""Opt-in, listing-field-only exclusions for a personal radar profile."""
	exclude_internships: bool = False
	exclude_hourly_daily_pay: bool = False

	@property
	def enabled(self) -> bool:
		return self.exclude_internships or self.exclude_hourly_daily_pay


@dataclass(frozen=True)
class RadarProfile:
	name: str
	queries: tuple[str, ...]
	cities: tuple[RadarCity, ...]
	max_pages_per_query: int = 10
	city_tiers: tuple[RadarCityTier, ...] = ()
	max_requests: int = 400
	delay_range: tuple[float, float] = (8.0, 15.0)
	output_dir: str = "output"
	max_retries: int = 1
	retry_delay_seconds: float = 60.0
	# ``native_joblist`` is the established default. ``page_capture`` is opt-in:
	# it drives the BOSS page's own result list and must never silently fall back
	# to a direct job-list request.
	boss_search_transport: str = "native_joblist"
	# ``page_capture`` has two independently observed BOSS layouts.  This
	# is an explicit route choice, never an error-driven fallback.
	boss_page_layout: str = "current_city"
	# An optional query-level observation window.  It is intentionally sparse:
	# unspecified queries retain the established city/tier/default resolution.
	query_page_limits: Mapping[str, int] = field(default_factory=dict)
	job_exclusions: RadarJobExclusions = field(default_factory=RadarJobExclusions)
	# Keep the established AI-PM gate by default. Other modes intentionally
	# receive isolated fingerprints/baselines and should use a separate DB.
	relevance_mode: str = "ai_pm"
	# Command-line --max-pages sets this transient field so a one-off uniform
	# scan still receives its own baseline fingerprint without rewriting YAML.
	page_limit_override: int | None = None

	def page_limit_for_city(self, city: RadarCity, query: str | None = None) -> int:
		if self.page_limit_override is not None:
			return self.page_limit_override
		if query is not None and query in self.query_page_limits:
			return self.query_page_limits[query]
		if city.max_pages_per_query is not None:
			return city.max_pages_per_query
		for tier in self.city_tiers:
			if tier.name == city.tier:
				return tier.max_pages_per_query
		return self.max_pages_per_query

	@property
	def city_page_limits(self) -> dict[str, int]:
		return {city.name: self.page_limit_for_city(city) for city in self.cities}

	@property
	def search_page_limits(self) -> dict[str, int]:
		"""Effective configured cap for every city/query search combination."""
		return {
			f"{city.name} / {query}": self.page_limit_for_city(city, query)
			for query in self.queries
			for city in self.cities
		}

	def with_uniform_page_limit(self, pages: int) -> RadarProfile:
		limit = _positive_int(pages, "max_pages_per_query", maximum=100)
		return replace(self, page_limit_override=limit)

	@property
	def fingerprint(self) -> str:
		# Preserve the established fingerprint for legacy profiles so adding tier
		# support does not silently invalidate an existing baseline. Tiered and
		# per-city configurations fingerprint their effective page window.
		legacy_page_plan = not self.query_page_limits and not self.city_tiers and all(
			not city.tier and city.max_pages_per_query is None for city in self.cities
		)
		if legacy_page_plan:
			payload = {
				"name": self.name,
				"queries": list(self.queries),
				"cities": [{"name": city.name, "code": city.code} for city in self.cities],
				"max_pages_per_query": self.page_limit_override or self.max_pages_per_query,
				"relevance_version": "ai-pm-relevance-v2",
			}
		elif not self.query_page_limits:
			payload = {
				"name": self.name,
				"queries": list(self.queries),
				"cities": [
					{
						"name": city.name,
						"code": city.code,
						"max_pages_per_query": self.page_limit_for_city(city),
					}
					for city in self.cities
				],
				"relevance_version": "ai-pm-relevance-v2",
			}
		else:
			payload = {
				"name": self.name,
				"queries": list(self.queries),
				# A query-specific page plan is still executed against concrete
				# platform city codes.  Keep those identifiers in the baseline
				# identity: changing a code while retaining the display name must
				# never resume or compare against the old city's observations.
				"cities": [{"name": city.name, "code": city.code} for city in self.cities],
				"search_page_limits": self.search_page_limits,
				"relevance_version": "ai-pm-relevance-v2",
			}
		# Keep native profiles' established fingerprint intact. A page-capture
		# profile is a different observation route, so it must get an isolated
		# baseline and can never resume a native-joblist checkpoint.
		if self.boss_search_transport != "native_joblist":
			payload["boss_search_transport"] = self.boss_search_transport
			payload["boss_page_layout"] = self.boss_page_layout
		if self.job_exclusions.enabled:
			payload["job_exclusions"] = {
				"exclude_internships": self.job_exclusions.exclude_internships,
				"exclude_hourly_daily_pay": self.job_exclusions.exclude_hourly_daily_pay,
			}
		# Do not add the default to preserve every established AI-PM fingerprint.
		if self.relevance_mode != "ai_pm":
			payload["relevance_mode"] = self.relevance_mode
			payload["relevance_version"] = f"{self.relevance_mode}-relevance-v1"
		raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
		return hashlib.sha256(raw).hexdigest()

	@classmethod
	def from_dict(cls, raw: dict[str, Any], *, expected_name: str | None = None) -> RadarProfile:
		name = str(raw.get("name") or "").strip()
		if expected_name and name != expected_name:
			raise RadarProfileError(f"profile name must be {expected_name!r}, got {name!r}")
		if not _PROFILE_NAME_PATTERN.fullmatch(name):
			raise RadarProfileError("profile name may contain only letters, digits, underscores, and hyphens")

		queries = _unique_nonempty_strings(raw.get("queries"), "queries")
		if not queries:
			raise RadarProfileError("queries must contain at least one search term")

		city_tiers = _parse_city_tiers(raw.get("city_tiers"))
		declared_tiers = {tier.name for tier in city_tiers}

		raw_cities = raw.get("cities")
		if not isinstance(raw_cities, list) or not raw_cities:
			raise RadarProfileError("cities must contain at least one city")
		if len(raw_cities) > _MAX_CITIES:
			raise RadarProfileError(f"cities may contain at most {_MAX_CITIES} entries")
		cities: list[RadarCity] = []
		seen_names: set[str] = set()
		seen_codes: set[str] = set()
		for index, item in enumerate(raw_cities, start=1):
			if not isinstance(item, dict):
				raise RadarProfileError(f"cities[{index}] must be an object")
			city_name = str(item.get("name") or "").strip()
			city_code = str(item.get("code") or "").strip()
			city_tier = str(item.get("tier") or "").strip()
			if not city_name:
				raise RadarProfileError(f"cities[{index}].name must not be empty")
			if not _CITY_CODE_PATTERN.fullmatch(city_code):
				raise RadarProfileError(f"cities[{index}].code must be a 9-digit BOSS city code")
			if city_code == "100010000":
				raise RadarProfileError("the unreliable national city code 100010000 is not allowed")
			if city_name in seen_names or city_code in seen_codes:
				raise RadarProfileError(f"duplicate city in profile: {city_name} ({city_code})")
			if city_tier and city_tier not in declared_tiers:
				raise RadarProfileError(f"cities[{index}].tier references unknown city_tiers entry: {city_tier}")
			city_page_limit = None
			if item.get("max_pages_per_query") is not None:
				city_page_limit = _positive_int(
					item["max_pages_per_query"],
					f"cities[{index}].max_pages_per_query",
					maximum=100,
				)
			seen_names.add(city_name)
			seen_codes.add(city_code)
			cities.append(
				RadarCity(
					name=city_name,
					code=city_code,
					tier=city_tier,
					max_pages_per_query=city_page_limit,
				)
			)

		max_pages = _positive_int(raw.get("max_pages_per_query", 10), "max_pages_per_query", maximum=100)
		max_requests = _positive_int(raw.get("max_requests", 400), "max_requests", maximum=1000)
		delay_range = _number_range(raw.get("delay_range", [8.0, 15.0]), "delay_range")
		max_retries = _nonnegative_int(raw.get("max_retries", 1), "max_retries", maximum=3)
		retry_delay = _nonnegative_float(raw.get("retry_delay_seconds", 60.0), "retry_delay_seconds")
		boss_search_transport = str(raw.get("boss_search_transport", "native_joblist") or "").strip()
		if boss_search_transport not in _BOSS_SEARCH_TRANSPORTS:
			allowed = ", ".join(sorted(_BOSS_SEARCH_TRANSPORTS))
			raise RadarProfileError(f"boss_search_transport must be one of: {allowed}")
		boss_page_layout = str(raw.get("boss_page_layout", "current_city") or "").strip()
		if boss_page_layout not in _BOSS_PAGE_LAYOUTS:
			allowed_layouts = ", ".join(sorted(_BOSS_PAGE_LAYOUTS))
			raise RadarProfileError(f"boss_page_layout must be one of: {allowed_layouts}")
		output_dir = str(raw.get("output_dir") or "output").strip()
		if not output_dir:
			raise RadarProfileError("output_dir must not be empty")
		query_page_limits = _parse_query_page_limits(raw.get("query_page_limits"), queries)
		job_exclusions = _parse_job_exclusions(raw.get("job_exclusions"))
		relevance_mode = str(raw.get("relevance_mode", "ai_pm") or "").strip()
		try:
			relevance_mode = normalize_relevance_mode(relevance_mode)
		except ValueError as exc:
			allowed = ", ".join(sorted(RELEVANCE_MODES))
			raise RadarProfileError(f"relevance_mode must be one of: {allowed}") from exc

		return cls(
			name=name,
			queries=queries,
			cities=tuple(cities),
			max_pages_per_query=max_pages,
			city_tiers=city_tiers,
			max_requests=max_requests,
			delay_range=delay_range,
			output_dir=output_dir,
			max_retries=max_retries,
			retry_delay_seconds=retry_delay,
			boss_search_transport=boss_search_transport,
			boss_page_layout=boss_page_layout,
			query_page_limits=query_page_limits,
			job_exclusions=job_exclusions,
			relevance_mode=relevance_mode,
		)


def profile_path(data_dir: Path, profile_name: str) -> Path:
	_validate_profile_name(profile_name)
	return data_dir / "radar" / "profiles" / f"{profile_name}.yaml"


def ensure_profile_file(data_dir: Path, profile_name: str) -> Path:
	path = profile_path(data_dir, profile_name)
	if path.exists():
		return path
	template = Path(__file__).with_name("profiles") / f"{profile_name}.yaml"
	if not template.exists():
		raise RadarProfileError(f"radar profile {profile_name!r} does not exist; create it at {path}")
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
	return path


def load_radar_profile(data_dir: Path, profile_name: str) -> RadarProfile:
	path = ensure_profile_file(data_dir, profile_name)
	try:
		raw = yaml.safe_load(path.read_text(encoding="utf-8"))
	except (OSError, yaml.YAMLError) as exc:
		raise RadarProfileError(f"failed to read radar profile {path}: {exc}") from exc
	if not isinstance(raw, dict):
		raise RadarProfileError(f"radar profile {path} must contain a YAML object")
	return RadarProfile.from_dict(raw, expected_name=profile_name)


def resolve_output_dir(data_dir: Path, profile: RadarProfile) -> Path:
	configured = Path(profile.output_dir).expanduser()
	base = configured if configured.is_absolute() else data_dir / "radar" / configured
	# A one-off CLI page limit is a separate observation window, not a new
	# version of the configured daily delivery.  Keep its latest/daily/success
	# aliases away from the normal workbook even when it finishes successfully.
	# The same profile's SQLite history remains shared: an actually observed job
	# must not become "new" merely because we later return to the normal depth.
	if profile.page_limit_override is not None:
		return base / "overrides" / profile.fingerprint
	return base


def _validate_profile_name(profile_name: str) -> None:
	if not _PROFILE_NAME_PATTERN.fullmatch(profile_name):
		raise RadarProfileError("profile name may contain only letters, digits, underscores, and hyphens")


def _unique_nonempty_strings(value: Any, field_name: str) -> tuple[str, ...]:
	if not isinstance(value, list):
		raise RadarProfileError(f"{field_name} must be a list")
	result: list[str] = []
	seen: set[str] = set()
	for item in value:
		text = str(item or "").strip()
		if text and text not in seen:
			seen.add(text)
			result.append(text)
	return tuple(result)


def _positive_int(value: Any, field_name: str, *, maximum: int) -> int:
	try:
		parsed = int(value)
	except (TypeError, ValueError) as exc:
		raise RadarProfileError(f"{field_name} must be an integer") from exc
	if parsed < 1 or parsed > maximum:
		raise RadarProfileError(f"{field_name} must be between 1 and {maximum}")
	return parsed


def _nonnegative_int(value: Any, field_name: str, *, maximum: int) -> int:
	try:
		parsed = int(value)
	except (TypeError, ValueError) as exc:
		raise RadarProfileError(f"{field_name} must be an integer") from exc
	if parsed < 0 or parsed > maximum:
		raise RadarProfileError(f"{field_name} must be between 0 and {maximum}")
	return parsed


def _nonnegative_float(value: Any, field_name: str) -> float:
	try:
		parsed = float(value)
	except (TypeError, ValueError) as exc:
		raise RadarProfileError(f"{field_name} must be a number") from exc
	if parsed < 0:
		raise RadarProfileError(f"{field_name} must not be negative")
	return parsed


def _number_range(value: Any, field_name: str) -> tuple[float, float]:
	if not isinstance(value, (list, tuple)) or len(value) != 2:
		raise RadarProfileError(f"{field_name} must contain exactly two numbers")
	low = _nonnegative_float(value[0], f"{field_name}[0]")
	high = _nonnegative_float(value[1], f"{field_name}[1]")
	if low > high:
		raise RadarProfileError(f"{field_name} minimum must not exceed maximum")
	return low, high


def _parse_city_tiers(value: Any) -> tuple[RadarCityTier, ...]:
	if value is None:
		return ()
	if not isinstance(value, dict):
		raise RadarProfileError("city_tiers must be an object")
	tiers: list[RadarCityTier] = []
	for raw_name, raw_settings in value.items():
		name = str(raw_name or "").strip()
		if not _TIER_NAME_PATTERN.fullmatch(name):
			raise RadarProfileError("city_tiers names may contain only letters, digits, underscores, hyphens, or Chinese")
		if isinstance(raw_settings, dict):
			if "max_pages_per_query" not in raw_settings:
				raise RadarProfileError(f"city_tiers.{name}.max_pages_per_query is required")
			raw_limit = raw_settings["max_pages_per_query"]
		else:
			# A numeric shorthand keeps small personal configurations readable.
			raw_limit = raw_settings
		limit = _positive_int(raw_limit, f"city_tiers.{name}.max_pages_per_query", maximum=100)
		tiers.append(RadarCityTier(name=name, max_pages_per_query=limit))
	return tuple(tiers)


def _parse_query_page_limits(value: Any, queries: tuple[str, ...]) -> dict[str, int]:
	if value is None:
		return {}
	if not isinstance(value, dict):
		raise RadarProfileError("query_page_limits must be an object")
	result: dict[str, int] = {}
	known = set(queries)
	for raw_query, raw_limit in value.items():
		query = str(raw_query or "").strip()
		if not query:
			raise RadarProfileError("query_page_limits contains an empty query")
		if query not in known:
			raise RadarProfileError(f"query_page_limits references query not in queries: {query}")
		result[query] = _positive_int(raw_limit, f"query_page_limits.{query}", maximum=100)
	return result


def _parse_job_exclusions(value: Any) -> RadarJobExclusions:
	if value is None:
		return RadarJobExclusions()
	if not isinstance(value, dict):
		raise RadarProfileError("job_exclusions must be an object")
	allowed = {"exclude_internships", "exclude_hourly_daily_pay"}
	unknown = set(value) - allowed
	if unknown:
		raise RadarProfileError(f"job_exclusions contains unknown setting: {sorted(unknown)[0]}")
	return RadarJobExclusions(
		exclude_internships=_strict_bool(value.get("exclude_internships", False), "job_exclusions.exclude_internships"),
		exclude_hourly_daily_pay=_strict_bool(value.get("exclude_hourly_daily_pay", False), "job_exclusions.exclude_hourly_daily_pay"),
	)


def _strict_bool(value: Any, field_name: str) -> bool:
	if not isinstance(value, bool):
		raise RadarProfileError(f"{field_name} must be true or false")
	return value
