import ipaddress
import hashlib
import json
import re
import socket
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone

from project_management.models import (
    FundingOpportunity,
    FundingOpportunityRevision,
    ProjectFundingMatch,
)


class FundingSourceSecurityError(ValueError):
    pass


class FundingSourcePayloadError(ValueError):
    pass


class FundingSourceUnavailable(RuntimeError):
    pass


def validate_official_source_url(url, allowed_hosts):
    if not isinstance(url, str) or len(url) > 2000:
        raise FundingSourceSecurityError("invalid_url")
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    configured_hosts = {
        str(host).rstrip(".").lower() for host in allowed_hosts if host
    }
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in configured_hosts
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise FundingSourceSecurityError("unapproved_source_url")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise FundingSourceSecurityError("private_source_address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _tokens(value):
    return set(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _resolve_addresses(hostname):
    return sorted(
        {
            row[4][0]
            for row in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    )


class OfficialFundingSourceAdapter:
    def __init__(
        self,
        source,
        *,
        requester=None,
        resolver=None,
        max_bytes=1_000_000,
        timeout_seconds=5,
    ):
        self.source = source
        self.requester = requester or requests.Session()
        self.resolver = resolver or _resolve_addresses
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    def _record_failure(self, code):
        checked_at = timezone.now()
        type(self.source).objects.filter(pk=self.source.pk).update(
            last_checked_at=checked_at,
            last_error=code,
        )
        self.source.last_checked_at = checked_at
        self.source.last_error = code

    def _record_success(self):
        checked_at = timezone.now()
        type(self.source).objects.filter(pk=self.source.pk).update(
            last_checked_at=checked_at,
            last_success_at=checked_at,
            last_error="",
        )
        self.source.last_checked_at = checked_at
        self.source.last_success_at = checked_at
        self.source.last_error = ""

    def fetch(self, url):
        try:
            safe_url = validate_official_source_url(url, self.source.allowed_hosts)
        except FundingSourceSecurityError as exc:
            self._record_failure(str(exc))
            raise
        hostname = urlsplit(safe_url).hostname
        try:
            addresses = self.resolver(hostname)
        except (OSError, socket.gaierror) as exc:
            self._record_failure("source_resolution_failed")
            raise FundingSourceUnavailable("source_resolution_failed") from exc
        if not addresses:
            self._record_failure("source_resolution_failed")
            raise FundingSourceUnavailable("source_resolution_failed")
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                self._record_failure("invalid_resolved_address")
                raise FundingSourceSecurityError("invalid_resolved_address") from exc
            if not address.is_global:
                self._record_failure("private_resolved_address")
                raise FundingSourceSecurityError("private_resolved_address")
        try:
            response = self.requester.get(
                safe_url,
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
                headers={"Accept": "text/html"},
            )
        except requests.Timeout as exc:
            self._record_failure("source_timeout")
            raise FundingSourceUnavailable("source_timeout") from exc
        except requests.RequestException as exc:
            self._record_failure("source_unavailable")
            raise FundingSourceUnavailable("source_unavailable") from exc
        if 300 <= response.status_code < 400:
            self._record_failure("source_redirect_rejected")
            raise FundingSourceSecurityError("source_redirect_rejected")
        if response.status_code != 200:
            self._record_failure("source_http_error")
            raise FundingSourceUnavailable("source_http_error")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            self._record_failure("unsupported_content_type")
            raise FundingSourcePayloadError("unsupported_content_type")
        content = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) > self.max_bytes:
                    self._record_failure("source_content_too_large")
                    raise FundingSourcePayloadError("source_content_too_large")
        except requests.RequestException as exc:
            self._record_failure("source_unavailable")
            raise FundingSourceUnavailable("source_unavailable") from exc
        self._record_success()
        return bytes(content).decode(response.encoding or "utf-8", errors="replace")

    def normalize(self, payload):
        if not isinstance(payload, dict):
            raise FundingSourcePayloadError("invalid_payload")
        source_url = validate_official_source_url(
            payload.get("source_url"),
            self.source.allowed_hosts,
        )
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 500:
            raise FundingSourcePayloadError("invalid_title")
        retrieved_at = payload.get("retrieved_at")
        if isinstance(retrieved_at, str):
            retrieved_at = parse_datetime(retrieved_at)
        if not retrieved_at:
            raise FundingSourcePayloadError("invalid_retrieved_at")
        if timezone.is_naive(retrieved_at):
            retrieved_at = timezone.make_aware(retrieved_at)

        dates = {}
        for key in ("application_start", "application_end"):
            value = payload.get(key)
            if isinstance(value, str):
                value = parse_date(value)
                if value is None:
                    raise FundingSourcePayloadError(f"invalid_{key}")
            dates[key] = value

        evidence = payload.get("evidence") or []
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise FundingSourcePayloadError("invalid_evidence")
        normalized_evidence = []
        for item in evidence:
            if not isinstance(item, dict):
                raise FundingSourcePayloadError("invalid_evidence")
            excerpt = item.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > 2000:
                raise FundingSourcePayloadError("invalid_evidence_excerpt")
            if any(ord(character) < 32 and character not in "\n\r\t" for character in excerpt):
                raise FundingSourcePayloadError("invalid_evidence_excerpt")
            evidence_url = validate_official_source_url(
                item.get("source_url") or source_url,
                self.source.allowed_hosts,
            )
            normalized_evidence.append(
                {"excerpt": excerpt.strip(), "source_url": evidence_url}
            )
        rules = payload.get("eligibility_rules") or {}
        if not isinstance(rules, dict):
            raise FundingSourcePayloadError("invalid_eligibility_rules")
        return {
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "title": title,
            "summary": str(payload.get("summary") or "").strip(),
            "application_start": dates["application_start"],
            "application_end": dates["application_end"],
            "eligibility_rules": rules,
            "evidence": normalized_evidence,
        }


@transaction.atomic
def ingest_funding_opportunity(source, external_id, normalized):
    opportunity, _ = FundingOpportunity.objects.get_or_create(
        source=source,
        external_id=external_id,
    )
    content_payload = {
        key: value
        for key, value in normalized.items()
        if key != "retrieved_at"
    }
    content_hash = hashlib.sha256(
        json.dumps(content_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    revision, created = FundingOpportunityRevision.objects.get_or_create(
        opportunity=opportunity,
        content_hash=content_hash,
        defaults={**normalized},
    )
    if created or opportunity.current_revision_id != revision.id:
        opportunity.current_revision = revision
        opportunity.save(update_fields=["current_revision", "updated_at"])
    return revision


def sync_project_funding_match(project, revision, actor=None):
    result = evaluate_funding_match(
        project,
        {
            "title": revision.title,
            "summary": revision.summary,
            "eligibility_rules": revision.eligibility_rules,
            "retrieved_at": revision.retrieved_at,
        },
        freshness_days=getattr(settings, "FUNDING_SOURCE_FRESHNESS_DAYS", 30),
    )
    threshold = getattr(settings, "FUNDING_MATCH_THRESHOLD", 60)
    if result["relevance_score"] < threshold:
        ProjectFundingMatch.objects.filter(project=project, revision=revision).delete()
        return None
    match, _ = ProjectFundingMatch.objects.update_or_create(
        project=project,
        revision=revision,
        defaults={
            "relevance_score": result["relevance_score"],
            "eligibility_state": result["eligibility_state"],
            "match_reasons": result["match_reasons"],
            "eligibility_conditions": result["conditions"],
            "created_by": actor,
        },
    )
    return match


def evaluate_funding_match(project, opportunity, freshness_days=30):
    project_tokens = _tokens(
        " ".join(
            filter(
                None,
                [project.project_name, project.project_description, project.country],
            )
        )
    )
    opportunity_tokens = _tokens(
        " ".join(
            filter(None, [opportunity.get("title"), opportunity.get("summary")])
        )
    )
    shared = sorted(project_tokens & opportunity_tokens)
    relevance_score = min(100, len(shared) * 20)
    conditions = []
    failed = False
    unknown = False
    rules = opportunity.get("eligibility_rules") or {}

    regions = rules.get("regions")
    if regions:
        state = "passed" if project.country in regions else "failed"
        failed = failed or state == "failed"
        conditions.append(
            {
                "key": "region",
                "state": state,
                "expected": regions,
                "actual": project.country,
            }
        )

    minimum_budget = rules.get("minimum_budget")
    if minimum_budget is not None:
        if project.contract_value is None:
            state = "unknown"
            unknown = True
        else:
            state = "passed" if project.contract_value >= minimum_budget else "failed"
            failed = failed or state == "failed"
        conditions.append(
            {
                "key": "minimum_budget",
                "state": state,
                "expected": minimum_budget,
                "actual": project.contract_value,
            }
        )

    for key in sorted(set(rules) - {"regions", "minimum_budget"}):
        unknown = True
        conditions.append(
            {
                "key": key,
                "state": "unknown",
                "expected": rules[key],
                "actual": None,
            }
        )

    retrieved_at = opportunity.get("retrieved_at")
    is_stale = not retrieved_at or retrieved_at < timezone.now() - timedelta(
        days=freshness_days
    )
    if failed:
        eligibility_state = "disqualified"
    elif unknown or not rules:
        eligibility_state = "unknown"
    else:
        eligibility_state = "eligible"
    return {
        "relevance_score": relevance_score,
        "eligibility_state": eligibility_state,
        "match_reasons": shared,
        "conditions": conditions,
        "is_stale": is_stale,
    }
