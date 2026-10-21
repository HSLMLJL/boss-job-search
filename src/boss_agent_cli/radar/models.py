from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from boss_agent_cli.api.models import employment_type_from_raw
from boss_agent_cli.radar_core import (
	content_fingerprint,
	fallback_identity_key,
	family_fingerprint,
)


def utc_now() -> datetime:
	return datetime.now(timezone.utc)


def _string_list(value: Any) -> tuple[str, ...]:
	if not isinstance(value, (list, tuple)):
		return ()
	return tuple(str(item).strip() for item in value if str(item).strip())


def _parse_source_time(value: Any) -> datetime | None:
	if value is None or value == "":
		return None
	if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
		number = float(value)
		if number > 10_000_000_000:
			number /= 1000
		try:
			return datetime.fromtimestamp(number, tz=timezone.utc)
		except (OverflowError, OSError, ValueError):
			return None
	text = str(value).strip()
	if not text:
		return None
	try:
		parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return None
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class JobObservation:
	platform: str
	query: str
	shard: str
	title: str
	company: str
	city: str
	platform_job_id: str = ""
	security_id: str = ""
	district: str = ""
	salary: str = ""
	experience: str = ""
	education: str = ""
	skills: tuple[str, ...] = ()
	welfare: tuple[str, ...] = ()
	job_labels: tuple[str, ...] = ()
	employment_type: str = ""
	description: str = ""
	published_at: datetime | None = None
	observed_at: datetime = field(default_factory=utc_now)
	source_page: int = 0
	source_rank: int = 0

	@property
	def identity_key(self) -> str:
		"""Exact listing identity. Volatile security_id is deliberately excluded."""
		if self.platform_job_id:
			return f"id:{self.platform_job_id}"
		return fallback_identity_key(
			source=self.platform,
			company=self.company,
			title=self.title,
			city=self.city,
			district=self.district,
			experience=self.experience,
			education=self.education,
		)

	@property
	def family_key(self) -> str:
		"""Conservative cross-listing signature used only to flag possible reposts."""
		return family_fingerprint(
			source=self.platform,
			company=self.company,
			title=self.title,
			city=self.city,
			district=self.district,
		)

	@property
	def content_hash(self) -> str:
		return content_fingerprint(
			title=self.title,
			company=self.company,
			city=self.city,
			district=self.district,
			salary=self.salary,
			experience=self.experience,
			education=self.education,
			skills=self.skills,
			welfare=self.welfare,
			labels=self.job_labels,
			# The platform-neutral identity helper intentionally has no employment
			# type argument.  The displayed title and listing fields remain the
			# stable content contract; the type is a collection policy input.
			description=self.description,
		)

	@property
	def job_url(self) -> str:
		if self.platform_job_id:
			return f"https://www.zhipin.com/job_detail/{quote(self.platform_job_id, safe='')}.html"
		return ""

	def to_public_dict(self) -> dict[str, Any]:
		return {
			"job_id": self.platform_job_id,
			"title": self.title,
			"company": self.company,
			"city": self.city,
			"district": self.district,
			"salary": self.salary,
			"experience": self.experience,
			"education": self.education,
			"employment_type": self.employment_type,
			"published_at": self.published_at.isoformat() if self.published_at else None,
			"first_discovered_at": self.observed_at.isoformat(),
			"ai_evidence": list(ai_relevance_evidence(self)),
			"ai_evidence_details": list(ai_relevance_evidence_details(self)),
			"url": self.job_url,
		}

	@classmethod
	def from_boss(
		cls,
		raw: dict[str, Any],
		*,
		query: str,
		shard: str = "",
		observed_at: datetime | None = None,
		source_page: int = 0,
		source_rank: int = 0,
	) -> JobObservation:
		published_at = None
		# Do not reinterpret create/update timestamps as publication time.
		for key in ("jobPublishTime", "publishTime"):
			published_at = _parse_source_time(raw.get(key))
			if published_at is not None:
				break
		return cls(
			platform="zhipin",
			query=query,
			shard=shard,
			platform_job_id=str(raw.get("encryptJobId") or ""),
			security_id=str(raw.get("securityId") or ""),
			title=str(raw.get("jobName") or ""),
			company=str(raw.get("brandName") or ""),
			city=str(raw.get("cityName") or ""),
			district=str(raw.get("areaDistrict") or ""),
			salary=str(raw.get("salaryDesc") or ""),
			experience=str(raw.get("jobExperience") or ""),
			education=str(raw.get("jobDegree") or ""),
			skills=_string_list(raw.get("skills")),
			welfare=_string_list(raw.get("welfareList")),
			job_labels=_string_list(raw.get("jobLabels")),
			employment_type=employment_type_from_raw(raw.get("jobType")),
			description=str(raw.get("postDescription") or raw.get("jobDescription") or ""),
			published_at=published_at,
			observed_at=observed_at or utc_now(),
			source_page=source_page,
			source_rank=source_rank,
		)


@dataclass(frozen=True)
class RadarEvent:
	event_type: str
	job: JobObservation
	previous_job_id: str = ""

	def to_dict(self) -> dict[str, Any]:
		data = self.job.to_public_dict()
		data["event_type"] = self.event_type
		if self.previous_job_id:
			data["previous_job_id"] = self.previous_job_id
		return data


AI_PM_QUERY_TERMS = (
	"AI产品经理",
	"人工智能产品经理",
	"大模型产品经理",
	"AIGC产品经理",
	"智能体产品经理",
	"Agent产品经理",
)

# A profile selects one of these listing-field-only gates.  Search query text is
# deliberately never evidence: it may help BOSS find a listing, but it must not
# make an otherwise unrelated listing eligible for delivery.
RELEVANCE_MODES = frozenset({
        "ai_pm",
        "product_entry",
        "product_ops",
        "implementation_success",
        "content_ecommerce",
        # Explicit opt-in collection pool for offline human classification.  It
        # deliberately does not infer relevance from a search query or attach
        # synthetic evidence to a listing.
        "broad_listing_pool",
})

_PRODUCT_ROLE_PATTERN = re.compile(
	r"(产品经理|产品负责人|产品专家|产品总监|产品主管|产品owner|product\s*manager)",
	re.IGNORECASE,
)
_AI_PATTERN = re.compile(
	r"(人工智能|大模型|生成式|智能体|算法产品|AIGC|"
	r"(?<![A-Za-z])AI(?![A-Za-z])|(?<![A-Za-z])Agent(?![A-Za-z])|"
	r"(?<![A-Za-z])LLM(?![A-Za-z])|(?<![A-Za-z])RAG(?![A-Za-z]))",
	re.IGNORECASE,
)
_PRODUCT_ENTRY_ROLE_PATTERN = re.compile(
        r"(产品(?:经理)?助理|助理产品经理|初级产品经理|产品专员|产品培训生|产品校招生|associate\s+product)",
        re.IGNORECASE,
)
_PRODUCT_OPS_ROLE_PATTERN = re.compile(
        r"(产品运营(?:专员|助理|经理|主管)?|product\s*operations?)",
        re.IGNORECASE,
)
_IMPLEMENTATION_SUCCESS_ROLE_PATTERN = re.compile(
        r"(实施(?:顾问|工程师|专员|经理|项目经理)?|项目实施|交付(?:顾问|专员|经理|工程师)?|"
        r"客户成功(?:专员|经理|顾问)?|customer\s*success)",
        re.IGNORECASE,
)
_CUSTOMER_SUCCESS_PATTERN = re.compile(
        r"(客户成功(?:专员|经理|顾问)?|customer\s*success)", re.IGNORECASE,
)
_SOFTWARE_CONTEXT_PATTERN = re.compile(
        r"(软件|SaaS|系统(?:实施|配置|上线|集成)?|信息化|数字化|企业服务|CRM|ERP|"
        r"云服务|云平台|业务平台|产品配置)",
        re.IGNORECASE,
)
_CONSTRUCTION_IMPLEMENTATION_PATTERN = re.compile(
        r"(施工|土建|机电|工程监理|建筑工程|装修工程|工地)", re.IGNORECASE,
)
_CONTENT_ECOMMERCE_ROLE_PATTERN = re.compile(
        r"(内容运营|电商运营|店铺运营|直播运营|新媒体运营|content\s*operations?|"
        r"e-?commerce\s*operations?)",
        re.IGNORECASE,
)


_TITLE_INTERNSHIP_PATTERN = re.compile(r"实习(?:生|岗(?:位)?|职位)?|\bintern(?:ship)?\b", re.IGNORECASE)
_TITLE_PARALLEL_INTERNSHIP_OPTION_PATTERN = re.compile(
	r"(?:(?:全职|校招)\s*(?:[/／、,，]|\b(?:or|and)\b)\s*实习|"
	r"实习\s*(?:[/／、,，]|\b(?:or|and)\b)\s*(?:全职|校招))",
	re.IGNORECASE,
)
_TITLE_MENTORS_INTERNS_PATTERN = re.compile(r"(?:带|管理|指导|辅导|培养)\s*实习生")
_CHINESE_HOURLY_DAILY_PAY_PATTERN = re.compile(
	r"\d+(?:\.\d+)?(?:\s*[-~至]\s*\d+(?:\.\d+)?)?\s*(?:元|¥|￥)?\s*(?:/|每)\s*(?:小时|时|天|日)"
)
_ENGLISH_HOURLY_DAILY_PAY_PATTERN = re.compile(
	r"(?:¥|￥|\$|rmb|cny)?\s*\d+(?:\.\d+)?(?:\s*[-~]\s*(?:¥|￥|\$)?\s*\d+(?:\.\d+)?)?\s*"
	r"(?:元|rmb|cny)?\s*(?:/|per\s+)(?:hours?|hrs?|h|days?|d)\b",
	re.IGNORECASE,
)
_SALARY_HOURLY_DAILY_LABEL_PATTERN = re.compile(r"日结|时薪|日薪|按(?:天|日|小时|时)(?:结算|计薪|计算)?")


def radar_job_exclusion_reasons(
	observation: JobObservation,
	*,
	exclude_internships: bool = False,
	exclude_hourly_daily_pay: bool = False,
) -> tuple[str, ...]:
	"""Return explicit collection-policy reasons without reading a JD as policy.

	Internship exclusion uses only the platform's normalized job type or the
	listing title.  A title that expressly offers a full-time/campus option, or
	only says the role manages interns, stays eligible for human review.
	Hourly/daily exclusion is deliberately limited to the salary field.
	"""
	reasons: list[str] = []
	if exclude_internships:
		title = observation.title.strip()
		if observation.employment_type == "实习":
			reasons.append("岗位类型明确为实习")
		elif (
			_TITLE_INTERNSHIP_PATTERN.search(title)
			and not _TITLE_PARALLEL_INTERNSHIP_OPTION_PATTERN.search(title)
			and not _TITLE_MENTORS_INTERNS_PATTERN.search(title)
		):
			reasons.append("岗位标题明确为实习")
	if exclude_hourly_daily_pay and (
		_CHINESE_HOURLY_DAILY_PAY_PATTERN.search(observation.salary)
		or _ENGLISH_HOURLY_DAILY_PAY_PATTERN.search(observation.salary)
		or _SALARY_HOURLY_DAILY_LABEL_PATTERN.search(observation.salary)
	):
		reasons.append("薪资明确按小时或按天计")
	return tuple(reasons)


def normalize_relevance_mode(value: str) -> str:
        mode = str(value or "ai_pm").strip()
        if mode not in RELEVANCE_MODES:
                allowed = ", ".join(sorted(RELEVANCE_MODES))
                raise ValueError(f"unsupported relevance_mode {mode!r}; expected one of: {allowed}")
        return mode


def _listing_evidence(observation: JobObservation) -> str:
        return " ".join(
                [
                        observation.title,
                        " ".join(observation.skills),
                        " ".join(observation.job_labels),
                        observation.description,
                ]
        )


def is_relevant(
        observation: JobObservation,
        *,
        relevance_mode: str = "ai_pm",
        exclude_internships: bool = False,
        exclude_hourly_daily_pay: bool = False,
) -> bool:
        """High-recall, profile-selected relevance gate using listing-owned fields only."""
        mode = normalize_relevance_mode(relevance_mode)
        if radar_job_exclusion_reasons(
                observation,
                exclude_internships=exclude_internships,
                exclude_hourly_daily_pay=exclude_hourly_daily_pay,
        ):
                return False
        if mode == "broad_listing_pool":
                return True
        title = observation.title
        evidence = _listing_evidence(observation)
        if mode == "ai_pm":
                return bool(_PRODUCT_ROLE_PATTERN.search(title) and _AI_PATTERN.search(evidence))
        if mode == "product_entry":
                return bool(_PRODUCT_ENTRY_ROLE_PATTERN.search(title))
        if mode == "product_ops":
                return bool(_PRODUCT_OPS_ROLE_PATTERN.search(title))
        if mode == "implementation_success":
                if not _IMPLEMENTATION_SUCCESS_ROLE_PATTERN.search(title):
                        return False
                # Customer-success titles are a coherent target family even when
                # BOSS omits a SaaS/product tag.  Exclude only listings with an
                # explicit construction signal and no software signal; uncertain
                # implementation rows remain for delivery-side human review.
                if _CUSTOMER_SUCCESS_PATTERN.search(title):
                        return True
                return not (
                        _CONSTRUCTION_IMPLEMENTATION_PATTERN.search(evidence)
                        and not _SOFTWARE_CONTEXT_PATTERN.search(evidence)
                )
        return bool(_CONTENT_ECOMMERCE_ROLE_PATTERN.search(title))


def is_ai_pm_relevant(
        observation: JobObservation,
        *,
        exclude_internships: bool = False,
        exclude_hourly_daily_pay: bool = False,
) -> bool:
        """Compatibility wrapper for the established AI product-manager gate."""
        return is_relevant(
                observation,
                relevance_mode="ai_pm",
                exclude_internships=exclude_internships,
                exclude_hourly_daily_pay=exclude_hourly_daily_pay,
        )


def relevance_evidence(observation: JobObservation, *, relevance_mode: str = "ai_pm") -> tuple[str, ...]:
        """Persist only compact listing-owned evidence needed to reapply a gate."""
        mode = normalize_relevance_mode(relevance_mode)
        if mode == "broad_listing_pool":
                return ()
        if mode == "ai_pm":
                return ai_relevance_evidence(observation)
        if mode == "product_entry":
                patterns = (_PRODUCT_ENTRY_ROLE_PATTERN,)
                values = (observation.title,)
        elif mode == "product_ops":
                patterns = (_PRODUCT_OPS_ROLE_PATTERN,)
                values = (observation.title,)
        elif mode == "implementation_success":
                patterns = (_IMPLEMENTATION_SUCCESS_ROLE_PATTERN, _SOFTWARE_CONTEXT_PATTERN)
                values = (observation.title, _listing_evidence(observation))
        else:
                patterns = (_CONTENT_ECOMMERCE_ROLE_PATTERN,)
                values = (observation.title,)
        matched: list[str] = []
        seen: set[str] = set()
        for value in values:
                for pattern in patterns:
                        for match in pattern.finditer(value):
                                term = match.group(0).strip()
                                if term and term.casefold() not in seen:
                                        matched.append(term)
                                        seen.add(term.casefold())
        return tuple(matched)


def ai_relevance_evidence(observation: JobObservation) -> tuple[str, ...]:
	"""Return a compact, auditable list of AI terms found in listing-owned fields."""
	evidence = " ".join(
		[
			observation.title,
			" ".join(observation.skills),
			" ".join(observation.job_labels),
			observation.description,
		]
	)
	result: list[str] = []
	seen: set[str] = set()
	for match in _AI_PATTERN.finditer(evidence):
		value = match.group(0).strip()
		normalized = value.casefold()
		if value and normalized not in seen:
			seen.add(normalized)
			result.append(value)
		if len(result) >= 5:
			break
	return tuple(result)


_AI_EVIDENCE_CONTEXT_RADIUS = 48
_AI_EVIDENCE_CONTEXT_MAXIMUM = 120


def ai_relevance_evidence_details(observation: JobObservation) -> tuple[dict[str, str], ...]:
	"""Describe where each stored AI term was found without retaining a full JD.

	Only listing-owned fields that participate in relevance are examined.  Search
	query, company, welfare, security identifiers, and other account-scoped data
	are deliberately absent: they can neither create evidence nor enter this
	persisted explanation.
	"""
	fields: tuple[tuple[str, tuple[str, ...]], ...] = (
		("title", (observation.title,)),
		("skills", observation.skills),
		("job_labels", observation.job_labels),
		("description", (observation.description,)),
	)
	result: list[dict[str, str]] = []
	seen: set[str] = set()
	for source_field, values in fields:
		for value in values:
			for match in _AI_PATTERN.finditer(value):
				term = match.group(0).strip()
				normalized = term.casefold()
				if not term or normalized in seen:
					continue
				seen.add(normalized)
				result.append({
					"term": term,
					"source_field": source_field,
					"context": _ai_evidence_context(value, match.start(), match.end()),
				})
				if len(result) >= 5:
					return tuple(result)
	return tuple(result)


def _ai_evidence_context(value: str, start: int, end: int) -> str:
	"""Keep a small, normalized listing fragment around a matched AI term."""
	left = max(0, start - _AI_EVIDENCE_CONTEXT_RADIUS)
	right = min(len(value), end + _AI_EVIDENCE_CONTEXT_RADIUS)
	fragment = re.sub(r"\s+", " ", value[left:right]).strip()
	if left:
		fragment = f"…{fragment}"
	if right < len(value):
		fragment = f"{fragment}…"
	return fragment[:_AI_EVIDENCE_CONTEXT_MAXIMUM]


def _safe_listing_text(value: str, *, maximum: int) -> str:
	"""Keep enough public listing context for offline triage, never a credential."""
	text = re.sub(r"\s+", " ", str(value or "")).strip()
	text = re.sub(
		r"(?i)\b(?:access[_ -]?token|authorization|cookie|security[_ -]?id|session[_ -]?id)\s*[:=]\s*[^\s,;]+",
		"[redacted]",
		text,
	)
	return text[:maximum]


def observation_json(observation: JobObservation, *, relevance_mode: str = "ai_pm") -> str:
	"""Store normalized listing data only; omit volatile access credentials."""
	mode = normalize_relevance_mode(relevance_mode)
	payload = observation.to_public_dict()
	payload.pop("url", None)
	payload["relevance_mode"] = mode
	payload["relevance_evidence"] = list(relevance_evidence(observation, relevance_mode=mode))
	if mode != "ai_pm":
		payload["listing_evidence"] = {
			"skills": [
				_safe_listing_text(value, maximum=160)
				for value in observation.skills[:24]
				if _safe_listing_text(value, maximum=160)
			],
			"job_labels": [
				_safe_listing_text(value, maximum=160)
				for value in observation.job_labels[:24]
				if _safe_listing_text(value, maximum=160)
			],
			"description_excerpt": _safe_listing_text(observation.description, maximum=600),
		}
	return json.dumps(payload, ensure_ascii=False, sort_keys=True)
