from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import WikidataConfig
from .metadata import normalize_text
from .models import AwardEvidence, EvidenceItem, ResearchedFact, ResearchResult
from .providers import ProviderError

WIKIDATA_FACT_FIELDS = {"wikidata_work_id"}


class WikidataAwardProvider:
    """Resolve a work and collect its structured award statements."""

    def __init__(self, config: WikidataConfig):
        self.config = config
        self.user_agent = f"calibre-research-bot/0.1 ({config.contact})"
        self._last_request_at: float | None = None
        self._entity_cache: dict[str, dict[str, Any]] = {}

    def research(self, *, title: str, author: str) -> ResearchResult:
        search = self._get(
            {
                "action": "wbsearchentities",
                "search": title,
                "language": "en",
                "type": "item",
                "limit": str(self.config.max_candidates),
            }
        )
        candidate_ids = [item["id"] for item in search.get("search", []) if item.get("id")]
        if not candidate_ids:
            return _empty_result(title, author)

        entities = self._entities(candidate_ids)
        author_ids = {
            value for entity in entities.values() for value in _claim_entity_ids(entity, "P50")
        }
        authors = self._entities(sorted(author_ids)) if author_ids else {}
        work_id = _select_work(
            title=title,
            author=author,
            candidate_ids=candidate_ids,
            entities=entities,
            authors=authors,
        )
        if work_id is None:
            return _empty_result(title, author)

        work = entities[work_id]
        winner_records = _award_records(work, "P166", "winner")
        winner_ids = {record["award_id"] for record in winner_records}
        nominee_records = [
            record
            for record in _award_records(work, "P1411", "nominee")
            if record["award_id"] not in winner_ids
        ]
        records = winner_records + nominee_records
        award_ids = sorted({record["award_id"] for record in records})
        award_entities = self._entities(award_ids) if award_ids else {}
        source_url = f"https://www.wikidata.org/wiki/{work_id}"
        evidence = EvidenceItem(
            source_type="structured_data",
            source_name="Wikidata",
            source_url=source_url,
            citation_text=f"Wikidata statements for {work_id}",
        )
        awards = [
            AwardEvidence(
                award_name=_label(award_entities.get(record["award_id"]), record["award_id"]),
                year=record["year"],
                category=None,
                result=record["result"],
                source_name="Wikidata",
                source_identifier=record["statement_id"],
                source_url=source_url,
                confidence=0.95,
            )
            for record in records
        ]
        return ResearchResult(
            title=title,
            author=author,
            identity_confidence=0.95,
            facts=[
                ResearchedFact(
                    field_name="wikidata_work_id",
                    value=work_id,
                    confidence=0.95,
                    evidence=[evidence],
                )
            ],
            awards=awards,
            estimated_cost_usd=0.0,
        )

    def _entities(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        missing = [entity_id for entity_id in ids if entity_id not in self._entity_cache]
        if missing:
            data = self._get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(missing),
                    "props": "labels|descriptions|claims",
                    "languages": "en",
                }
            )
            self._entity_cache.update(data.get("entities", {}))
        return {
            entity_id: self._entity_cache[entity_id]
            for entity_id in ids
            if entity_id in self._entity_cache
        }

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        query = urlencode(
            {
                **params,
                "format": "json",
                "maxlag": str(self.config.maxlag_seconds),
            }
        )
        url = f"{self.config.base_url}?{query}"
        for attempt in range(self.config.max_retries + 1):
            request = Request(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            self._pace_request()
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    data = json.load(response)
                    if data.get("error", {}).get("code") == "maxlag":
                        if attempt >= self.config.max_retries:
                            raise ProviderError("wikidata remained unavailable because of maxlag")
                        self._wait_before_retry(
                            attempt=attempt,
                            retry_after=_retry_after_seconds(getattr(response, "headers", None)),
                        )
                        continue
                    return data
            except HTTPError as exc:
                if exc.code not in {429, 503} or attempt >= self.config.max_retries:
                    raise ProviderError(f"wikidata request failed: {exc}") from exc
                self._wait_before_retry(
                    attempt=attempt,
                    retry_after=_retry_after_seconds(exc.headers),
                )
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= self.config.max_retries:
                    raise ProviderError(f"wikidata request failed: {exc}") from exc
                self._wait_before_retry(attempt=attempt, retry_after=None)
        raise AssertionError("unreachable")

    def _pace_request(self) -> None:
        if self.config.requests_per_second <= 0 or self._last_request_at is None:
            return
        minimum_interval = 1.0 / self.config.requests_per_second
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)

    def _wait_before_retry(self, *, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            time.sleep(retry_after)
            return
        delay = min(max(5.0, 2.0**attempt), self.config.retry_wait_max_seconds)
        time.sleep(delay)


def combine_results(*results: ResearchResult) -> ResearchResult:
    first = results[0]
    return ResearchResult(
        title=first.title,
        author=first.author,
        identity_confidence=max((result.identity_confidence for result in results), default=0.0),
        facts=[fact for result in results for fact in result.facts],
        claims=[claim for result in results for claim in result.claims],
        awards=[award for result in results for award in result.awards],
        estimated_cost_usd=sum(result.estimated_cost_usd for result in results),
    )


def _empty_result(title: str, author: str) -> ResearchResult:
    return ResearchResult(title=title, author=author, identity_confidence=0.0)


def _select_work(
    *,
    title: str,
    author: str,
    candidate_ids: list[str],
    entities: dict[str, dict[str, Any]],
    authors: dict[str, dict[str, Any]],
) -> str | None:
    wanted_author = normalize_text(author)
    for candidate_id in candidate_ids:
        entity = entities.get(candidate_id, {})
        if not _titles_match(_label(entity, ""), title):
            continue
        author_labels = {
            normalize_text(_label(authors.get(author_id), ""))
            for author_id in _claim_entity_ids(entity, "P50")
        }
        if wanted_author in author_labels:
            return candidate_id
    return None


def _titles_match(candidate: str, wanted: str) -> bool:
    candidate_full = normalize_text(candidate)
    wanted_full = normalize_text(wanted)
    if candidate_full == wanted_full:
        return True
    candidate_base = normalize_text(candidate.split(":", 1)[0])
    wanted_base = normalize_text(wanted.split(":", 1)[0])
    return candidate_full == wanted_base or wanted_full == candidate_base


def _claim_entity_ids(entity: dict[str, Any], property_id: str) -> list[str]:
    values: list[str] = []
    for statement in entity.get("claims", {}).get(property_id, []):
        value = statement.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            values.append(value["id"])
    return values


def _award_records(entity: dict[str, Any], property_id: str, result: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, statement in enumerate(entity.get("claims", {}).get(property_id, [])):
        value = statement.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            continue
        statement_id = str(statement.get("id") or f"{property_id}:{value['id']}:{index}")
        records.append(
            {
                "award_id": value["id"],
                "statement_id": statement_id,
                "result": result,
                "year": _statement_year(statement),
            }
        )
    return records


def _statement_year(statement: dict[str, Any]) -> int | None:
    qualifiers = statement.get("qualifiers", {})
    for property_id in ("P585", "P580"):
        for qualifier in qualifiers.get(property_id, []):
            value = qualifier.get("datavalue", {}).get("value", {})
            time_value = value.get("time") if isinstance(value, dict) else None
            if isinstance(time_value, str) and len(time_value) >= 5:
                try:
                    return int(time_value[1:5])
                except ValueError:
                    pass
    return None


def _label(entity: dict[str, Any] | None, fallback: str) -> str:
    if not entity:
        return fallback
    return str(entity.get("labels", {}).get("en", {}).get("value") or fallback)


def _retry_after_seconds(headers: Any) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
