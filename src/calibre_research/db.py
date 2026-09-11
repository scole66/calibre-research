from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .metadata import MetadataCandidate, MetadataProposal, dump_json, metadata_query_key


@dataclass(frozen=True)
class UpsertResult:
    work_id: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.work_id is not None


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = files("calibre_research").joinpath("schema.sql").read_text()
        with self.connect() as con:
            con.executescript(schema)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.create_function(
            "metadata_query_key",
            3,
            lambda title, author, isbn: metadata_query_key(
                title=str(title),
                author=str(author),
                isbn=str(isbn) if isbn is not None else None,
            ),
            deterministic=True,
        )
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def upsert_calibre_book(self, library: str, book: dict) -> UpsertResult:
        title = str(book.get("title") or "").strip()
        authors = book.get("authors") or book.get("author") or ""
        if isinstance(authors, list):
            author = " & ".join(str(value).strip() for value in authors if str(value).strip())
        else:
            author = str(authors).strip()

        if not title and not author:
            return UpsertResult(None, "missing title and author")
        if not title:
            return UpsertResult(None, "missing title")
        if not author:
            return UpsertResult(None, "missing author")

        language = _scalar_text(book.get("languages") or book.get("language"))

        with self.connect() as con:
            con.execute(
                """
                INSERT INTO works(canonical_title, canonical_author)
                VALUES (?, ?)
                ON CONFLICT(canonical_title, canonical_author) DO NOTHING
                """,
                (title, author),
            )
            work_id = con.execute(
                "SELECT id FROM works WHERE canonical_title=? AND canonical_author=?",
                (title, author),
            ).fetchone()["id"]

            con.execute(
                """
                INSERT INTO editions(
                    work_id, calibre_book_id, calibre_library, title, author,
                    isbn, publisher, publication_date, series, series_index, language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calibre_library, calibre_book_id) DO UPDATE SET
                    work_id=excluded.work_id,
                    title=excluded.title,
                    author=excluded.author,
                    isbn=excluded.isbn,
                    publisher=excluded.publisher,
                    publication_date=excluded.publication_date,
                    series=excluded.series,
                    series_index=excluded.series_index,
                    language=excluded.language,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    work_id,
                    int(book["id"]),
                    library,
                    title,
                    author,
                    _scalar_text(book.get("isbn")),
                    _scalar_text(book.get("publisher")),
                    _scalar_text(book.get("pubdate")),
                    _scalar_text(book.get("series")),
                    _float_or_none(book.get("series_index")),
                    language,
                ),
            )
            return UpsertResult(work_id)

    def metadata_candidates(
        self,
        *,
        providers: list[str],
        retry_errors: bool = False,
        refresh: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not providers:
            return []
        placeholders = ", ".join("?" for _ in providers)
        query = """
            SELECT
                e.id AS edition_id,
                e.work_id,
                e.calibre_book_id,
                e.calibre_library,
                e.title,
                e.author,
                e.isbn,
                e.publisher,
                e.publication_date,
                e.series,
                e.series_index,
                e.language
            FROM editions e
        """
        params: list[Any] = []
        if retry_errors:
            query += f"""
                WHERE EXISTS (
                    SELECT 1
                    FROM metadata_issues mi
                    WHERE mi.edition_id=e.id
                      AND mi.classification='PROVIDER_ERROR'
                      AND mi.status='OPEN'
                      AND mi.provider IN ({placeholders})
                )
            """
            params.extend(providers)
        elif not refresh:
            query += f"""
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM metadata_issues mi
                    WHERE mi.edition_id=e.id
                      AND mi.classification='PROVIDER_ERROR'
                      AND mi.status='OPEN'
                      AND mi.provider IN ({placeholders})
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM metadata_lookups ml
                    WHERE ml.edition_id=e.id
                      AND ml.status='MATCH'
                      AND ml.provider IN ({placeholders})
                      AND ml.query_key=metadata_query_key(e.title, e.author, e.isbn)
                )
                  AND (
                    SELECT COUNT(DISTINCT ml.provider)
                    FROM metadata_lookups ml
                    WHERE ml.edition_id=e.id
                      AND ml.status='NO_MATCH'
                      AND ml.provider IN ({placeholders})
                      AND ml.query_key=metadata_query_key(e.title, e.author, e.isbn)
                  ) < ?
            """
            params.extend(providers)
            params.extend(providers)
            params.extend(providers)
            params.append(len(providers))

        query += """
            ORDER BY
                CASE WHEN e.publication_date IS NULL OR e.publication_date = ''
                          OR e.publication_date LIKE '0101-01-01%'
                     THEN 0 ELSE 1 END,
                CASE WHEN e.isbn IS NULL OR e.isbn = '' THEN 0 ELSE 1 END,
                e.id
        """
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as con:
            return [dict(row) for row in con.execute(query, params).fetchall()]

    def open_metadata_error_providers(self, *, edition_id: int) -> set[str]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT provider
                FROM metadata_issues
                WHERE edition_id=?
                  AND classification='PROVIDER_ERROR'
                  AND status='OPEN'
                """,
                (edition_id,),
            ).fetchall()
        return {str(row["provider"]) for row in rows}

    def cached_metadata_lookup(
        self, *, edition_id: int, provider: str, query_key: str
    ) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT 
                    id,
                    source_url,
                    identity_confidence,
                    raw_json,
                    normalized_json,
                    status,
                    retrieved_at
                FROM metadata_lookups
                WHERE edition_id=? AND provider=? AND query_key=?
                """,
                (edition_id, provider, query_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "source_url": row["source_url"],
            "identity_confidence": row["identity_confidence"],
            "raw": json.loads(row["raw_json"]),
            "normalized": json.loads(row["normalized_json"]),
            "status": row["status"],
            "retrieved_at": row["retrieved_at"],
        }

    def store_metadata_lookup(self, *, edition_id: int, candidate: MetadataCandidate) -> int:
        raw = candidate.raw or {}
        normalized = candidate.normalized()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO metadata_lookups(
                    edition_id, provider, query_key, source_url,
                    identity_confidence, raw_json, normalized_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'MATCH')
                ON CONFLICT(edition_id, provider, query_key) DO UPDATE SET
                    source_url=excluded.source_url,
                    identity_confidence=excluded.identity_confidence,
                    raw_json=excluded.raw_json,
                    normalized_json=excluded.normalized_json,
                    status='MATCH',
                    retrieved_at=CURRENT_TIMESTAMP
                """,
                (
                    edition_id,
                    candidate.provider,
                    candidate.query_key,
                    candidate.source_url,
                    candidate.identity_confidence,
                    dump_json(raw),
                    dump_json(normalized),
                ),
            )
            return con.execute(
                """
                SELECT id FROM metadata_lookups
                WHERE edition_id=? AND provider=? AND query_key=?
                """,
                (edition_id, candidate.provider, candidate.query_key),
            ).fetchone()["id"]

    def store_metadata_miss(
        self, *, edition_id: int, provider: str, query_key: str, source_url: str = ""
    ) -> int:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO metadata_lookups(
                    edition_id, provider, query_key, source_url,
                    identity_confidence, raw_json, normalized_json, status
                ) VALUES (?, ?, ?, ?, NULL, '{}', '{}', 'NO_MATCH')
                ON CONFLICT(edition_id, provider, query_key) DO UPDATE SET
                    source_url=excluded.source_url,
                    identity_confidence=NULL,
                    raw_json='{}',
                    normalized_json='{}',
                    status='NO_MATCH',
                    retrieved_at=CURRENT_TIMESTAMP
                """,
                (edition_id, provider, query_key, source_url),
            )
            return con.execute(
                """
                SELECT id FROM metadata_lookups
                WHERE edition_id=? AND provider=? AND query_key=?
                """,
                (edition_id, provider, query_key),
            ).fetchone()["id"]

    def replace_metadata_proposals(
        self,
        *,
        edition_id: int,
        lookup_id: int,
        proposals: list[MetadataProposal],
    ) -> None:
        with self.connect() as con:
            con.execute(
                "DELETE FROM metadata_proposals WHERE edition_id=? AND status='PROPOSED'",
                (edition_id,),
            )
            for proposal in proposals:
                con.execute(
                    """
                    INSERT INTO metadata_proposals(
                        edition_id, lookup_id, field_name, current_value_json,
                        proposed_value_json, confidence, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edition_id,
                        lookup_id,
                        proposal.field_name,
                        dump_json(proposal.current_value),
                        dump_json(proposal.proposed_value),
                        proposal.confidence,
                        proposal.reason,
                    ),
                )

    def record_metadata_issue(
        self,
        *,
        edition_id: int,
        classification: str,
        provider: str | None,
        reason: str,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO metadata_issues(edition_id, classification, provider, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(edition_id, classification, provider, status) DO UPDATE SET
                    reason=excluded.reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (edition_id, classification, provider or "", reason),
            )

    def resolve_metadata_issues(self, *, edition_id: int) -> None:
        with self.connect() as con:
            # The schema permits one OPEN and one RESOLVED row for the same issue.
            # Remove a repeated OPEN occurrence before resolving so transitions such
            # as error -> unmatched -> error -> match cannot violate that constraint.
            con.execute(
                """
                DELETE FROM metadata_issues AS open_issue
                WHERE open_issue.edition_id=?
                  AND open_issue.status='OPEN'
                  AND EXISTS (
                    SELECT 1
                    FROM metadata_issues AS resolved_issue
                    WHERE resolved_issue.edition_id=open_issue.edition_id
                      AND resolved_issue.classification=open_issue.classification
                      AND resolved_issue.provider=open_issue.provider
                      AND resolved_issue.status='RESOLVED'
                  )
                """,
                (edition_id,),
            )
            con.execute(
                """
                UPDATE metadata_issues
                SET status='RESOLVED', updated_at=CURRENT_TIMESTAMP
                WHERE edition_id=? AND status='OPEN'
                """,
                (edition_id,),
            )


def _scalar_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        text = ", ".join(str(item).strip() for item in value if str(item).strip())
        return text or None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
