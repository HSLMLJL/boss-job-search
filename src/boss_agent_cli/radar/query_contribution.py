"""Offline, city-scoped query-contribution reporting for verified delivery bundles.

This module deliberately analyzes only immutable merged-bundle observations.  It
does not fetch jobs, decide relevance, or claim recall/completeness.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, cast


class QueryContributionError(ValueError):
	"""The bundle is missing the attribution required for a safe report."""


def build_multi_city_query_contribution(bundle: Mapping[str, Any]) -> dict[str, Any]:
	"""Report observed query overlap per city without collapsing cities together.

	A listing observed through two source-search cities is intentionally counted
	once in each city, while the global total uses platform + full platform ID.
	Every job
	source must name an existing source metadata record and at least one query;
	missing attribution is an error rather than an inferred label.
	"""
	jobs = _require_list(bundle, "jobs")
	sources_by_id = _source_metadata(bundle)
	city_role_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
	city_query_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
	city_query_roles: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
	city_query_source_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
	city_query_run_ids: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
	city_listing_ids: dict[str, set[str]] = defaultdict(set)
	global_ids: set[str] = set()
	input_job_keys: set[str] = set()
	# Declare every configured city/query even when it produced no accepted job, so
	# a zero-contribution query remains visible rather than silently disappearing.
	for source_id, metadata in sources_by_id.items():
		for search_city in metadata["cities"]:
			city_role_ids[search_city][metadata["role"]]
			for query in metadata["queries"]:
				city_query_ids[search_city][query]
				city_query_roles[search_city][query].add(metadata["role"])
				city_query_source_ids[search_city][query].add(source_id)
				city_query_run_ids[search_city][query].add(metadata["logical_scan_id"])

	for index, job in enumerate(jobs):
		if not isinstance(job, Mapping):
			raise QueryContributionError(f"jobs[{index}] must be an object")
		city = _required_text(job, "city", f"jobs[{index}]")
		platform = _required_text(job, "platform", f"jobs[{index}]")
		platform_job_id = _required_text(job, "platform_job_id", f"jobs[{index}]")
		# A source-search city supplies the grouping below.  The listing city is
		# intentionally not part of this identity, because listings can be returned
		# outside the city used to search.
		job_key = f"{platform}\x1f{platform_job_id}"
		global_key = job_key
		if job_key in input_job_keys:
			raise QueryContributionError(
				f"jobs[{index}] duplicates a complete platform identity already present in jobs"
			)
		input_job_keys.add(job_key)
		city_listing_ids[city].add(global_key)
		global_ids.add(global_key)
		job_sources = _require_list(job, "sources", context=f"jobs[{index}]")
		if not job_sources:
			raise QueryContributionError(f"jobs[{index}].sources must not be empty")
		for source_index, source in enumerate(job_sources):
			context = f"jobs[{index}].sources[{source_index}]"
			if not isinstance(source, Mapping):
				raise QueryContributionError(f"{context} must be an object")
			role = _required_role(source, context)
			source_id = _required_text(source, "source_id", context)
			source_meta = sources_by_id.get(source_id)
			if source_meta is None:
				raise QueryContributionError(f"{context}.source_id is not declared in coverage: {source_id!r}")
			if source_meta["role"] != role:
				raise QueryContributionError(f"{context} role conflicts with coverage metadata for {source_id!r}")
			positions = _required_positions(source, context)
			declared_queries = set(_required_queries(source, context))
			position_queries = {position["query"] for position in positions}
			if declared_queries != position_queries:
				raise QueryContributionError(
					f"{context}.queries must exactly equal its positions query set"
				)
			for position in positions:
				search_city = cast(str, position["city"])
				query = cast(str, position["query"])
				if search_city not in source_meta["cities"]:
					raise QueryContributionError(
						f"{context}.positions city is absent from coverage metadata: {search_city!r}"
					)
				if query not in source_meta["queries"]:
					raise QueryContributionError(
						f"{context}.positions query is absent from coverage metadata: {query!r}"
					)
				if position["run_id"] is not None and position["run_id"] not in source_meta["run_ids"]:
					raise QueryContributionError(
						f"{context}.positions run_id is absent from coverage metadata: {position['run_id']!r}"
					)
				city_role_ids[search_city][role].add(job_key)
				city_query_ids[search_city][query].add(job_key)
				city_query_roles[search_city][query].add(role)
				city_query_source_ids[search_city][query].add(source_id)
				city_query_run_ids[search_city][query].add(source_meta["logical_scan_id"])

	cities = [_city_report(
		city, city_role_ids[city], city_query_ids[city], city_query_roles[city],
		city_query_source_ids[city], city_query_run_ids[city],
	)
		for city in sorted(city_role_ids)]
	return {
		"schema_version": 1,
		"scope": {
			"kind": "offline_multi_city_query_contribution",
			"source_city_identity": "source search city + platform + complete platform_job_id",
			"global_identity": "platform + complete platform_job_id; city does not participate",
			"interpretation": "observed merged-bundle overlap only; not recall, all-city inventory, or a basis for automatic query removal",
		},
		"input": {
			"bundle_schema_version": bundle.get("schema_version"),
			"scan_date": bundle.get("scan_date"),
			"job_records": len(jobs),
			"source_bindings": [
				{
					"source_id": source_id,
					"logical_scan_id": sources_by_id[source_id]["logical_scan_id"],
					"profile_fingerprint": sources_by_id[source_id]["profile_fingerprint"],
				}
				for source_id in sorted(sources_by_id)
			],
		},
		"cities": cities,
		"listing_cities": [
			{"listing_city": city, "global_observed_unique_jobs": len(ids)}
			for city, ids in sorted(city_listing_ids.items())
		],
		"totals": {
			"city_count": len(cities),
			"source_city_scoped_observed_unique_jobs": sum(city["union_observed_unique_jobs"] for city in cities),
			"global_union_observed_unique_jobs": len(global_ids),
			"listing_city_scoped_observed_unique_jobs": sum(len(ids) for ids in city_listing_ids.values()),
			"main_observed_unique_jobs": sum(city["main_observed_unique_jobs"] for city in cities),
			"supplement_observed_unique_jobs": sum(city["supplement_observed_unique_jobs"] for city in cities),
			"supplement_incremental_vs_primary_jobs": sum(city["supplement_incremental_vs_primary_jobs"] for city in cities),
		},
	}


def _city_report(
	city: str,
	role_ids: Mapping[str, set[str]],
	query_ids: Mapping[str, set[str]],
	query_roles: Mapping[str, set[str]],
	query_source_ids: Mapping[str, set[str]],
	query_run_ids: Mapping[str, set[str]],
) -> dict[str, Any]:
	primary = set(role_ids.get("main", set()))
	supplement = set(role_ids.get("supplement", set()))
	all_observed = primary | supplement
	queries: list[dict[str, Any]] = []
	for query in sorted(query_ids):
		observed = query_ids[query]
		others = set().union(*(ids for other, ids in query_ids.items() if other != query))
		exclusive = observed - others
		queries.append({
			"query": query,
			"roles": sorted(query_roles[query]),
			"source_ids": sorted(query_source_ids[query]),
			"logical_scan_ids": sorted(query_run_ids[query]),
			"observed_unique_jobs": len(observed),
			"unique_vs_primary_jobs": len(observed - primary),
			"exclusive_vs_all_other_queries_jobs": len(exclusive),
			"overlap_with_other_queries_jobs": len(observed & others),
			"overlap_with_primary_jobs": len(observed & primary),
		})
	return {
		"city": city,
		"main_observed_unique_jobs": len(primary),
		"supplement_observed_unique_jobs": len(supplement),
		"supplement_incremental_vs_primary_jobs": len(supplement - primary),
		"supplement_only_observed_unique_jobs": len(supplement - primary),
		"primary_supplement_overlap_jobs": len(primary & supplement),
		"global_union_observed_unique_jobs": len(all_observed),
		"union_observed_unique_jobs": len(all_observed),
		"queries": queries,
	}


def _source_metadata(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
	coverage = bundle.get("coverage")
	if not isinstance(coverage, Mapping):
		raise QueryContributionError("coverage must be an object")
	result: dict[str, dict[str, Any]] = {}
	for role, field in (("main", "main_sources"), ("supplement", "supplement_sources")):
		for index, source in enumerate(_require_list(coverage, field, context="coverage")):
			context = f"coverage.{field}[{index}]"
			if not isinstance(source, Mapping):
				raise QueryContributionError(f"{context} must be an object")
			source_id = _required_text(source, "source_id", context)
			if source_id in result:
				raise QueryContributionError(f"duplicate coverage source_id: {source_id!r}")
			if _required_role(source, context) != role:
				raise QueryContributionError(f"{context}.role must be {role!r}")
			result[source_id] = {
				"role": role,
				"queries": set(_required_queries(source, context)),
				"cities": set(_required_text_list(source, "cities", context)),
				"logical_scan_id": _required_text(source, "logical_scan_id", context),
				"profile_fingerprint": _required_text(source, "profile_fingerprint", context),
				"run_ids": set(_required_text_list(source, "run_ids", context)),
			}
	if not result:
		raise QueryContributionError("coverage declares no sources")
	return result


def _require_list(value: Mapping[str, Any], field: str, *, context: str = "bundle") -> list[Any]:
	result = value.get(field)
	if not isinstance(result, list):
		raise QueryContributionError(f"{context}.{field} must be a list")
	return result


def _required_text(value: Mapping[str, Any], field: str, context: str) -> str:
	result = value.get(field)
	if not isinstance(result, str) or not result.strip():
		raise QueryContributionError(f"{context}.{field} must be non-empty text")
	return result.strip()


def _required_role(value: Mapping[str, Any], context: str) -> str:
	role = _required_text(value, "source" if "source" in value else "role", context)
	if role not in {"main", "supplement"}:
		raise QueryContributionError(f"{context} role must be 'main' or 'supplement'")
	return role


def _required_queries(value: Mapping[str, Any], context: str) -> tuple[str, ...]:
	raw = _require_list(value, "queries", context=context)
	queries = tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())
	if not queries or len(queries) != len(raw):
		raise QueryContributionError(f"{context}.queries must be a non-empty list of non-empty text")
	return tuple(dict.fromkeys(queries))


def _required_text_list(value: Mapping[str, Any], field: str, context: str) -> tuple[str, ...]:
	raw = _require_list(value, field, context=context)
	items = tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())
	if not items or len(items) != len(raw):
		raise QueryContributionError(f"{context}.{field} must be a non-empty list of non-empty text")
	return tuple(dict.fromkeys(items))


def _required_positions(value: Mapping[str, Any], context: str) -> tuple[dict[str, str | None], ...]:
	raw = _require_list(value, "positions", context=context)
	if not raw:
		raise QueryContributionError(f"{context}.positions must not be empty")
	positions: list[dict[str, str | None]] = []
	for index, item in enumerate(raw):
		position_context = f"{context}.positions[{index}]"
		if not isinstance(item, Mapping):
			raise QueryContributionError(f"{position_context} must be an object")
		positions.append({
			"city": _required_text(item, "city", position_context),
			"query": _required_text(item, "query", position_context),
			"run_id": _optional_text(item, "run_id", position_context),
		})
	return tuple(positions)


def _optional_text(value: Mapping[str, Any], field: str, context: str) -> str | None:
	result = value.get(field)
	if result is None:
		return None
	if not isinstance(result, str) or not result.strip():
		raise QueryContributionError(f"{context}.{field} must be non-empty text when present")
	return result.strip()
