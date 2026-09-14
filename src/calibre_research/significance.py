from __future__ import annotations

import html
import re
from typing import Any

from .models import EvidenceItem, ResearchedFact, ResearchResult, SignificanceClaim

CACHED_METADATA_FACT_FIELDS = {
    "google_average_rating",
    "google_categories",
    "google_description",
    "google_ratings_count",
    "original_publication_year",
}
CACHED_METADATA_CLAIM_CATEGORIES = {"reader_reception"}


def research_from_cached_metadata(
    *, title: str, author: str, lookups: list[dict[str, Any]]
) -> ResearchResult:
    """Extract significance evidence without making network requests."""
    facts: list[ResearchedFact] = []
    claims: list[SignificanceClaim] = []
    identity_confidences: list[float] = []
    google_books_extracted = False

    for lookup in lookups:
        provider = str(lookup["provider"])
        normalized = lookup["normalized"]
        raw = lookup["raw"]
        confidence = float(lookup["identity_confidence"])
        identity_confidences.append(confidence)
        evidence = _lookup_evidence(lookup)

        original_year = _year(normalized.get("original_publication_date"))
        if original_year is not None and not any(
            fact.field_name == "original_publication_year" for fact in facts
        ):
            facts.append(
                ResearchedFact(
                    field_name="original_publication_year",
                    value=original_year,
                    confidence=confidence,
                    note=f"Original publication year reported by {provider}",
                    evidence=[evidence],
                )
            )
        if provider == "googlebooks" and not google_books_extracted:
            _extract_google_books(
                raw=raw,
                confidence=confidence,
                evidence=evidence,
                facts=facts,
            )
            google_books_extracted = True

    return ResearchResult(
        title=title,
        author=author,
        identity_confidence=max(identity_confidences, default=0.0),
        facts=facts,
        claims=claims,
        estimated_cost_usd=0.0,
    )


def facts_by_name(result: ResearchResult) -> dict[str, Any]:
    return {fact.field_name: fact.value for fact in result.facts}


def claims_by_category(result: ResearchResult) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for claim in result.claims:
        grouped.setdefault(claim.category, []).append(
            {
                "claim": claim.claim,
                "result": _claim_result(claim),
            }
        )
    return grouped


def evidence_coverage(
    *, rubric: dict[str, Any], result: ResearchResult, facts: dict[str, Any]
) -> float:
    evidenced: set[str] = set()
    if any(claim.category == "major_awards" for claim in result.claims):
        evidenced.add("major_awards")
    components = rubric["components"]
    total_weight = sum(float(component["max"]) for component in components.values())
    covered_weight = sum(float(components[name]["max"]) for name in evidenced)
    if total_weight <= 0:
        return 0.0
    return round((covered_weight / total_weight) * result.identity_confidence, 3)


def build_about(*, facts: dict[str, Any]) -> str | None:
    """Return descriptive context, explicitly separate from significance evidence."""
    description = facts.get("google_description")
    if isinstance(description, str) and description:
        return _summary_sentence(description)
    return None


def build_why_read(*, claims: list[SignificanceClaim]) -> str:
    """Summarize only evidence-backed claims that can justify reading the work."""
    reasons = [claim.claim for claim in claims if claim.category == "major_awards"]
    if not reasons:
        return "No evidence-backed rationale available yet."
    return " ".join(reasons)


def _extract_google_books(
    *,
    raw: dict[str, Any],
    confidence: float,
    evidence: EvidenceItem,
    facts: list[ResearchedFact],
) -> None:
    selected = raw.get("selected") or {}
    info = selected.get("volumeInfo") or {}

    description = _clean_description(info.get("description"))
    if description:
        facts.append(
            ResearchedFact(
                field_name="google_description",
                value=description,
                confidence=confidence,
                note="Publisher-supplied or provider-supplied description; not critical consensus",
                evidence=[evidence],
            )
        )

    categories = [
        str(value).strip() for value in info.get("categories") or [] if str(value).strip()
    ]
    if categories:
        facts.append(
            ResearchedFact(
                field_name="google_categories",
                value=categories,
                confidence=confidence,
                evidence=[evidence],
            )
        )

    rating = info.get("averageRating")
    ratings_count = info.get("ratingsCount")
    if isinstance(rating, (int, float)):
        facts.append(
            ResearchedFact(
                field_name="google_average_rating",
                value=float(rating),
                confidence=confidence,
                evidence=[evidence],
            )
        )
    if isinstance(ratings_count, int):
        facts.append(
            ResearchedFact(
                field_name="google_ratings_count",
                value=ratings_count,
                confidence=confidence,
                evidence=[evidence],
            )
        )


def _claim_result(claim: SignificanceClaim) -> str | None:
    if claim.category != "major_awards":
        return None
    text = claim.claim.casefold()
    if "won " in text or "winner" in text:
        return "winner"
    if "nominated" in text or "nominee" in text or "finalist" in text:
        return "nominee"
    return None


def _lookup_evidence(lookup: dict[str, Any]) -> EvidenceItem:
    provider = str(lookup["provider"])
    source_name = "Google Books" if provider == "googlebooks" else "Open Library"
    return EvidenceItem(
        source_type="cached_metadata",
        source_name=source_name,
        source_url=lookup["source_url"],
        citation_text=f"Cached {source_name} metadata response",
    )


def _year(value: Any) -> int | None:
    match = re.match(r"^(\d{4})", str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1 <= year <= 9999 else None


def _clean_description(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text or None


def _summary_sentence(description: str, *, limit: int = 240) -> str:
    sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0]
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rstrip() + "…"
