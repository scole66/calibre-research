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
