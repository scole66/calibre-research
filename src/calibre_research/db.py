from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
import sqlite3

from dataclasses import dataclass


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
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def upsert_calibre_book(self, library: str, book: dict) -> UpsertResult:
        title = str(book.get("title") or "").strip()

        authors = book.get("authors")
        if isinstance(authors, list):
            author = " & ".join(str(a).strip() for a in authors if str(a).strip())
        else:
            author = str(authors or book.get("author") or "").strip()

        calibre_id = book.get("id")

        if calibre_id is None:
            return UpsertResult(None, "missing calibre id")

        if not title and not author:
            return UpsertResult(None, "missing title and author")

        if not title:
            return UpsertResult(None, "missing title")

        if not author:
            return UpsertResult(None, "missing author")

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

            # params = (
            #     work_id,
            #     book.get("id"),
            #     library,
            #     title,
            #     author,
            #     book.get("isbn"),
            #     book.get("publisher"),
            #     book.get("pubdate"),
            #     book.get("series"),
            #     book.get("series_index"),
            #     book.get("languages"),
            # )
            # 
            # for i, value in enumerate(params, start=1):
            #     print(i, repr(value), type(value))

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
                    book.get("isbn"),
                    book.get("publisher"),
                    book.get("pubdate"),
                    book.get("series"),
                    _float_or_none(book.get("series_index")),
                    scalar_text(book.get("languages")) or scalar_text(book.get("language")),
                ),
            )
            return UpsertResult(work_id)


def _float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def scalar_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value).strip() or None
