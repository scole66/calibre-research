from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database
from .metadata import normalize_isbn, normalize_text


class GoodreadsImportError(ValueError):
    pass


@dataclass(frozen=True)
class GoodreadsImportSummary:
    rows: int
    matched: int = 0
    created: int = 0
    ambiguous: int = 0
    already_imported: bool = False


@dataclass(frozen=True)
class GoodreadsReconciliation:
    source_work_id: int
    source_title: str
    target_work_id: int | None
    target_title: str | None
    match_method: str
    source_records: int
    blocked_reason: str | None = None


@dataclass(frozen=True)
class GoodreadsReconciliationSummary:
    candidates: tuple[GoodreadsReconciliation, ...]
    applied: int = 0

    @property
    def ready(self) -> int:
        return sum(
            candidate.target_work_id is not None and candidate.blocked_reason is None
            for candidate in self.candidates
        )

    @property
    def blocked(self) -> int:
        return len(self.candidates) - self.ready


def import_goodreads_csv(database: Database, path: Path) -> GoodreadsImportSummary:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GoodreadsImportError("Goodreads export must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"Title", "Author"}.issubset(reader.fieldnames):
        raise GoodreadsImportError("Goodreads export must contain Title and Author columns")
    rows = list(reader)

    with database.connect() as con:
        prior = con.execute(
            "SELECT row_count FROM source_imports WHERE source='goodreads' AND content_sha256=?",
            (digest,),
        ).fetchone()
        if prior is not None:
            return GoodreadsImportSummary(rows=prior["row_count"], already_imported=True)

        import_id = con.execute(
            """
            INSERT INTO source_imports(source, source_filename, content_sha256, row_count)
            VALUES ('goodreads', ?, ?, ?)
            """,
            (path.name, digest, len(rows)),
        ).lastrowid
        title_author_map, series_title_author_map, isbn_map = _work_indexes(con)
        matched = created = ambiguous = 0
        seen_record_ids: set[str] = set()

        for row_number, row in enumerate(rows, start=2):
            title = str(row.get("Title") or "").strip()
            author = str(row.get("Author") or "").strip()
            if not title or not author:
                raise GoodreadsImportError(f"row {row_number} is missing Title or Author")
            source_record_id = str(row.get("Book Id") or row_number).strip()
            if source_record_id in seen_record_ids:
                raise GoodreadsImportError(
                    f"duplicate Goodreads Book Id {source_record_id!r} at row {row_number}"
                )
            seen_record_ids.add(source_record_id)
            isbn = normalize_isbn(str(row.get("ISBN13") or row.get("ISBN") or ""))
            candidates, method = _match_work(
                title=title,
                author=author,
                isbn=isbn,
                title_author_map=title_author_map,
                series_title_author_map=series_title_author_map,
                isbn_map=isbn_map,
            )
            if len(candidates) > 1:
                work_id = None
                match_status = "AMBIGUOUS"
                ambiguous += 1
            elif candidates:
                work_id = next(iter(candidates))
                match_status = "MATCHED"
                matched += 1
            else:
                work_id = _create_work(con, title=title, author=author, row=row)
                match_status = "CREATED"
                method = "created"
                created += 1
                title_author_map.setdefault(
                    (normalize_text(title), normalize_text(author)), set()
                ).add(work_id)
                series_key = _series_title_author_key(title=title, author=author)
                if series_key is not None:
                    series_title_author_map.setdefault(series_key, set()).add(work_id)
                if isbn:
                    isbn_map.setdefault(isbn, set()).add(work_id)

            record_id = con.execute(
                """
                INSERT INTO source_records(
                    import_id, work_id, source_record_id, raw_json, match_status, match_method
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    work_id,
                    source_record_id,
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                    match_status,
                    method,
                ),
            ).lastrowid
            if work_id is None:
                con.execute(
                    """
                    INSERT INTO review_queue(work_id, kind, payload_json, reason)
                    VALUES (?, 'SOURCE_MATCH', ?, ?)
                    """,
                    (
                        min(candidates),
                        json.dumps(
                            {
                                "source": "goodreads",
                                "source_record_id": source_record_id,
                                "candidate_work_ids": sorted(candidates),
                                "row": row,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "Goodreads record matches multiple local works",
                    ),
                )
                continue
            _store_observations(
                con,
                record_id=record_id,
                work_id=work_id,
                row=row,
                isbn=isbn,
            )

    return GoodreadsImportSummary(
        rows=len(rows), matched=matched, created=created, ambiguous=ambiguous
    )


def reconcile_goodreads_works(
    database: Database, *, apply: bool = False
) -> GoodreadsReconciliationSummary:
    """Find and optionally merge safe Goodreads-created duplicate works.

    Automatic reconciliation is deliberately limited to Goodreads-created works
    without editions and a unique match to a Calibre-backed work. Works that
    already have research attached are reported but never merged automatically.
    """
    with database.connect() as con:
        target_title_author, target_series, target_isbns = _calibre_work_indexes(con)
        rows = con.execute(
            """
            SELECT w.id, w.canonical_title, w.canonical_author,
                   COUNT(DISTINCT sr.id) AS source_records
            FROM works w
            JOIN source_records sr ON sr.work_id=w.id
            WHERE sr.match_status='CREATED'
              AND NOT EXISTS (SELECT 1 FROM editions e WHERE e.work_id=w.id)
            GROUP BY w.id
            ORDER BY w.canonical_author, w.canonical_title, w.id
            """
        ).fetchall()
        candidates: list[GoodreadsReconciliation] = []
        for row in rows:
            isbns = {
                normalized
                for isbn_row in con.execute(
                    "SELECT isbn FROM owned_editions WHERE work_id=? AND isbn IS NOT NULL",
                    (row["id"],),
                )
                if (normalized := normalize_isbn(isbn_row["isbn"]))
            }
            matches: set[int] = set()
            method = "title_author"
            for isbn in isbns:
                matches.update(target_isbns.get(isbn, set()))
            if matches:
                method = "isbn"
            else:
                matches, method = _match_work(
                    title=row["canonical_title"],
                    author=row["canonical_author"],
                    isbn=None,
                    title_author_map=target_title_author,
                    series_title_author_map=target_series,
                    isbn_map=target_isbns,
                )

            if not matches:
                continue

            target_id = next(iter(matches)) if len(matches) == 1 else None
            target_title = None
            blocked_reason = None
            if len(matches) > 1:
                blocked_reason = (
                    f"matches multiple Calibre works: {', '.join(map(str, sorted(matches)))}"
                )
            elif target_id is not None:
                target_title = con.execute(
                    "SELECT canonical_title FROM works WHERE id=?", (target_id,)
                ).fetchone()["canonical_title"]
                blocked_reason = _automatic_merge_blocker(con, row["id"])
            candidates.append(
                GoodreadsReconciliation(
                    source_work_id=row["id"],
                    source_title=row["canonical_title"],
                    target_work_id=target_id,
                    target_title=target_title,
                    match_method=method,
                    source_records=row["source_records"],
                    blocked_reason=blocked_reason,
                )
            )

        applied = 0
        if apply:
            for candidate in candidates:
                if candidate.target_work_id is None or candidate.blocked_reason is not None:
                    continue
                _merge_goodreads_work(
                    con,
                    source_work_id=candidate.source_work_id,
                    target_work_id=candidate.target_work_id,
                    method=candidate.match_method,
                )
                applied += 1

    return GoodreadsReconciliationSummary(tuple(candidates), applied=applied)


def _calibre_work_indexes(con):
    title_author: dict[tuple[str, str], set[int]] = {}
    series_title_author: dict[tuple[str, str], set[int]] = {}
    for row in con.execute(
        """
        SELECT DISTINCT w.id, w.canonical_title, w.canonical_author
        FROM works w JOIN editions e ON e.work_id=w.id
        """
    ):
        key = (normalize_text(row["canonical_title"]), normalize_text(row["canonical_author"]))
        title_author.setdefault(key, set()).add(row["id"])
        series_key = _series_title_author_key(
            title=row["canonical_title"], author=row["canonical_author"]
        )
        if series_key is not None:
            series_title_author.setdefault(series_key, set()).add(row["id"])
    isbn_map: dict[str, set[int]] = {}
    for row in con.execute(
        """
        SELECT e.work_id, e.isbn FROM editions e
        WHERE e.isbn IS NOT NULL AND e.isbn != ''
        """
    ):
        if isbn := normalize_isbn(row["isbn"]):
            isbn_map.setdefault(isbn, set()).add(row["work_id"])
    return title_author, series_title_author, isbn_map


def _automatic_merge_blocker(con, work_id: int) -> str | None:
    protected_tables = (
        "editions",
        "evidence",
        "facts",
        "significance_claims",
        "award_evidence",
        "scores",
        "derived_scores",
        "significance_provider_attempts",
    )
    populated = [
        table
        for table in protected_tables
        if con.execute(f"SELECT 1 FROM {table} WHERE work_id=? LIMIT 1", (work_id,)).fetchone()
    ]
    if populated:
        return "has non-Goodreads data in " + ", ".join(populated)
    return None


def _merge_goodreads_work(con, *, source_work_id: int, target_work_id: int, method: str) -> None:
    for table in (
        "reading_status_observations",
        "rating_observations",
        "owned_editions",
        "tag_observations",
        "review_queue",
    ):
        con.execute(
            f"UPDATE {table} SET work_id=? WHERE work_id=?",
            (target_work_id, source_work_id),
        )
    con.execute(
        """
        UPDATE source_records
        SET work_id=?, match_status='MATCHED', match_method=?
        WHERE work_id=?
        """,
        (target_work_id, f"reconciled_{method}", source_work_id),
    )
    con.execute("DELETE FROM works WHERE id=?", (source_work_id,))


def _work_indexes(
    con,
) -> tuple[
    dict[tuple[str, str], set[int]],
    dict[tuple[str, str], set[int]],
    dict[str, set[int]],
]:
    title_author: dict[tuple[str, str], set[int]] = {}
    series_title_author: dict[tuple[str, str], set[int]] = {}
    for row in con.execute("SELECT id, canonical_title, canonical_author FROM works"):
        key = (normalize_text(row["canonical_title"]), normalize_text(row["canonical_author"]))
        title_author.setdefault(key, set()).add(row["id"])
        series_key = _series_title_author_key(
            title=row["canonical_title"], author=row["canonical_author"]
        )
        if series_key is not None:
            series_title_author.setdefault(series_key, set()).add(row["id"])

    isbn_map: dict[str, set[int]] = {}
    for row in con.execute("SELECT work_id, isbn FROM editions WHERE isbn IS NOT NULL"):
        isbn = normalize_isbn(row["isbn"])
        if isbn:
            isbn_map.setdefault(isbn, set()).add(row["work_id"])
    for row in con.execute("SELECT work_id, isbn FROM owned_editions WHERE isbn IS NOT NULL"):
        isbn = normalize_isbn(row["isbn"])
        if isbn:
            isbn_map.setdefault(isbn, set()).add(row["work_id"])
    return title_author, series_title_author, isbn_map


def _match_work(
    *,
    title: str,
    author: str,
    isbn: str | None,
    title_author_map: dict[tuple[str, str], set[int]],
    series_title_author_map: dict[tuple[str, str], set[int]],
    isbn_map: dict[str, set[int]],
) -> tuple[set[int], str]:
    if isbn and isbn in isbn_map:
        return set(isbn_map[isbn]), "isbn"
    key = (normalize_text(title), normalize_text(author))
    exact = set(title_author_map.get(key, set()))
    if exact:
        return exact, "title_author"
    series_aliases = set(series_title_author_map.get(key, set()))
    if series_aliases:
        return series_aliases, "title_author_without_series_suffix"
    series_key = _series_title_author_key(title=title, author=author)
    if series_key is not None:
        candidates = set(title_author_map.get(series_key, set()))
        candidates.update(series_title_author_map.get(series_key, set()))
        if candidates:
            return candidates, "title_author_without_series_suffix"
    return set(), "title_author"


def _series_title_author_key(*, title: str, author: str) -> tuple[str, str] | None:
    base_title = re.sub(
        r"\s*\([^()]*(?:,\s*)?#\s*\d+(?:\.\d+)?\)\s*$",
        "",
        title,
    ).strip()
    if base_title == title.strip():
        return None
    return normalize_text(base_title), normalize_text(author)


def _create_work(con, *, title: str, author: str, row: dict[str, Any]) -> int:
    original_year = _positive_int(row.get("Original Publication Year"))
    con.execute(
        """
        INSERT INTO works(canonical_title, canonical_author, original_publication_date)
        VALUES (?, ?, ?)
        ON CONFLICT(canonical_title, canonical_author) DO NOTHING
        """,
        (title, author, str(original_year) if original_year else None),
    )
    return con.execute(
        "SELECT id FROM works WHERE canonical_title=? AND canonical_author=?",
        (title, author),
    ).fetchone()["id"]


def _store_observations(con, *, record_id: int, work_id: int, row: dict, isbn: str | None) -> None:
    status = _reading_status(row.get("Exclusive Shelf"))
    if status:
        con.execute(
            """
            INSERT INTO reading_status_observations(
                source_record_id, work_id, status, date_read
            ) VALUES (?, ?, ?, ?)
            """,
            (record_id, work_id, status, _text_or_none(row.get("Date Read"))),
        )
    rating = _positive_float(row.get("My Rating"))
    if rating is not None:
        con.execute(
            """
            INSERT INTO rating_observations(source_record_id, work_id, rating, scale_max)
            VALUES (?, ?, ?, 5.0)
            """,
            (record_id, work_id, rating),
        )
    owned_count = _positive_int(row.get("Owned Copies"))
    if owned_count:
        con.execute(
            """
            INSERT INTO owned_editions(
                source_record_id, work_id, format, isbn, owned_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (record_id, work_id, _text_or_none(row.get("Binding")), isbn, owned_count),
        )
    tags = {tag.strip() for tag in str(row.get("Bookshelves") or "").split(",") if tag.strip()}
    for tag in sorted(tags):
        con.execute(
            "INSERT INTO tag_observations(source_record_id, work_id, tag) VALUES (?, ?, ?)",
            (record_id, work_id, tag),
        )


def _reading_status(value: Any) -> str | None:
    shelf = normalize_text(str(value or "")).replace(" ", "-")
    return {
        "to-read": "unread",
        "currently-reading": "currently_reading",
        "read": "read",
        "did-not-finish": "abandoned",
        "abandoned": "abandoned",
    }.get(shelf)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(str(value or "0").strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(str(value or "0").strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
