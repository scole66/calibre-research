from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

UNKNOWN_DATE_PREFIX = "0101-01-01"
SUSPICIOUS_VALUES = {"unknown", "unknown author", "unknown title", "n/a", "none"}


@dataclass(frozen=True)
class MetadataCandidate:
    provider: str
    query_key: str
    source_url: str
    identity_confidence: float
    title: str | None = None
    authors: list[str] | None = None
    isbn: str | None = None
    publisher: str | None = None
    original_publication_date: str | None = None
    edition_publication_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None
    raw: dict[str, Any] | None = None

    def normalized(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "query_key": self.query_key,
            "source_url": self.source_url,
            "identity_confidence": self.identity_confidence,
            "title": self.title,
            "authors": self.authors,
            "isbn": self.isbn,
            "publisher": self.publisher,
            "original_publication_date": self.original_publication_date,
            "edition_publication_date": self.edition_publication_date,
            "language": self.language,
            "series": self.series,
            "series_index": self.series_index,
        }


@dataclass(frozen=True)
class MetadataProposal:
    field_name: str
    current_value: Any
    proposed_value: Any
    confidence: float
    reason: str


def is_missing(value: Any, *, field_name: str | None = None) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        if field_name == "publication_date" and stripped.startswith(UNKNOWN_DATE_PREFIX):
            return True
    if isinstance(value, (list, tuple, set, dict)) and not value:
        return True
    return False


def build_proposals(
    edition: dict[str, Any], candidate: MetadataCandidate
) -> list[MetadataProposal]:
    """Build conservative metadata-fill proposals.

    Milestone 1 only proposes values for fields that are structurally missing. It does
    not attempt to correct populated fields yet; that belongs to metadata audit.
    """

    proposals: list[MetadataProposal] = []
    confidence = candidate.identity_confidence

    mappings = [
        ("title", candidate.title),
        ("author", " & ".join(candidate.authors or []) or None),
        ("isbn", candidate.isbn),
        ("publisher", candidate.publisher),
        ("language", candidate.language),
        ("series", candidate.series),
        ("series_index", candidate.series_index),
    ]

    for field_name, proposed in mappings:
        current = edition.get(field_name)
        if is_missing(current, field_name=field_name) and not is_missing(
            proposed, field_name=field_name
        ):
            proposals.append(
                MetadataProposal(
                    field_name=field_name,
                    current_value=current,
                    proposed_value=proposed,
                    confidence=confidence,
                    reason="field is missing in Calibre metadata",
                )
            )

    current_pubdate = edition.get("publication_date")
    proposed_pubdate = candidate.original_publication_date or candidate.edition_publication_date
    if is_missing(current_pubdate, field_name="publication_date") and proposed_pubdate:
        proposals.append(
            MetadataProposal(
                field_name="publication_date",
                current_value=current_pubdate,
                proposed_value=proposed_pubdate,
                confidence=confidence,
                reason=(
                    "field is missing; using original publication date"
                    if candidate.original_publication_date
                    else "field is missing; using edition publication date"
                ),
            )
        )

    return proposals


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def candidate_from_normalized(
    data: dict[str, Any], *, raw: dict[str, Any] | None = None
) -> MetadataCandidate:
    return MetadataCandidate(
        provider=str(data["provider"]),
        query_key=str(data["query_key"]),
        source_url=str(data["source_url"]),
        identity_confidence=float(data["identity_confidence"]),
        title=data.get("title"),
        authors=data.get("authors"),
        isbn=data.get("isbn"),
        publisher=data.get("publisher"),
        original_publication_date=data.get("original_publication_date"),
        edition_publication_date=data.get("edition_publication_date"),
        language=data.get("language"),
        series=data.get("series"),
        series_index=data.get("series_index"),
        raw=raw,
    )


def classify_unresolved(edition: dict[str, Any], *, provider_error: bool = False) -> str:
    if provider_error:
        return "PROVIDER_ERROR"

    title = normalize_text(str(edition.get("title") or ""))
    author = normalize_text(str(edition.get("author") or ""))

    if not title or title in SUSPICIOUS_VALUES:
        return "MISSING_OR_SUSPICIOUS_TITLE"
    if not author or author in SUSPICIOUS_VALUES:
        return "MISSING_OR_SUSPICIOUS_AUTHOR"
    return "UNMATCHED_GENERIC"
