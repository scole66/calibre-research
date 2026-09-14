from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import WikidataConfig
from .metadata import normalize_text
from .models import EvidenceItem, ResearchedFact, ResearchResult, SignificanceClaim
from .providers import USER_AGENT_BASE, ProviderError

WIKIDATA_FACT_FIELDS = {"wikidata_work_id"}
WIKIDATA_CLAIM_CATEGORIES = {"major_awards"}


class WikidataAwardProvider:
    """Resolve a work and collect its structured award statements."""

    def __init__(self, config: WikidataConfig):
        self.config = config

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
        winners = set(_claim_entity_ids(work, "P166"))
        nominees = set(_claim_entity_ids(work, "P1411")) - winners
        award_ids = sorted(winners | nominees)
        awards = self._entities(award_ids) if award_ids else {}
        source_url = f"https://www.wikidata.org/wiki/{work_id}"
        evidence = EvidenceItem(
            source_type="structured_data",
            source_name="Wikidata",
            source_url=source_url,
            citation_text=f"Wikidata statements for {work_id}",
        )
        claims = [
            SignificanceClaim(
                category="major_awards",
                claim=f"Won the {_label(awards.get(award_id), award_id)}.",
                confidence=0.95,
                evidence=[evidence],
            )
            for award_id in sorted(winners)
        ]
        claims.extend(
            SignificanceClaim(
                category="major_awards",
                claim=f"Nominated for the {_label(awards.get(award_id), award_id)}.",
                confidence=0.95,
                evidence=[evidence],
            )
            for award_id in sorted(nominees)
        )
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
            claims=claims,
            estimated_cost_usd=0.0,
        )

    def _entities(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        data = self._get(
            {
                "action": "wbgetentities",
                "ids": "|".join(ids),
                "props": "labels|descriptions|claims",
                "languages": "en",
            }
        )
        return data.get("entities", {})

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.config.base_url}?{urlencode({**params, 'format': 'json'})}"
        request = Request(
            url, headers={"User-Agent": USER_AGENT_BASE, "Accept": "application/json"}
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"wikidata request failed: {exc}") from exc


def combine_results(*results: ResearchResult) -> ResearchResult:
    first = results[0]
    return ResearchResult(
        title=first.title,
        author=first.author,
        identity_confidence=max((result.identity_confidence for result in results), default=0.0),
        facts=[fact for result in results for fact in result.facts],
        claims=[claim for result in results for claim in result.claims],
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


def _label(entity: dict[str, Any] | None, fallback: str) -> str:
    if not entity:
        return fallback
    return str(entity.get("labels", {}).get("en", {}).get("value") or fallback)
