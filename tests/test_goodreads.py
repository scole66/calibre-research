from pathlib import Path

from typer.testing import CliRunner

import calibre_research.cli as cli_module
from calibre_research.db import Database
from calibre_research.goodreads import import_goodreads_csv

CSV_HEADER = (
    "Book Id,Title,Author,ISBN,ISBN13,My Rating,Binding,Original Publication Year,"
    "Date Read,Bookshelves,Exclusive Shelf,Owned Copies\n"
)


def _write_export(path: Path) -> None:
    path.write_text(
        CSV_HEADER + '10,Dreamsnake,Vonda N. McIntyre,,="9781234567890",5,Paperback,1978,'
        "2020/01/02,science-fiction,read,0\n"
        + '11,A Local Habitation,Seanan McGuire,,="9780756405960",0,Hardcover,2010,'
        ',"fantasy, urban-fantasy",to-read,1\n'
    )


def test_goodreads_import_is_idempotent_and_preserves_personal_evidence(tmp_path: Path):
    database = Database(tmp_path / "library.sqlite3")
    database.initialize()
    existing = database.upsert_calibre_book(
        "/library",
        {
            "id": "1",
            "title": "Dreamsnake",
            "authors": ["Vonda N. McIntyre"],
            "isbn": "9781234567890",
        },
    )
    assert existing.ok
    export = tmp_path / "goodreads_library_export.csv"
    _write_export(export)

    first = import_goodreads_csv(database, export)
    second = import_goodreads_csv(database, export)

    assert first.rows == 2
    assert first.matched == 1
    assert first.created == 1
    assert first.ambiguous == 0
    assert second.already_imported
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM source_imports").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM reading_status_observations").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM rating_observations").fetchone()[0] == 1
        ownership = con.execute(
            """
            SELECT w.canonical_title, oe.format, oe.isbn, oe.owned_count
            FROM owned_editions oe JOIN works w ON w.id=oe.work_id
            """
        ).fetchone()
        dreamsnake = con.execute(
            """
            SELECT rso.status, rso.date_read, ro.rating
            FROM works w
            JOIN reading_status_observations rso ON rso.work_id=w.id
            JOIN rating_observations ro ON ro.work_id=w.id
            WHERE w.canonical_title='Dreamsnake'
            """
        ).fetchone()
    assert dict(ownership) == {
        "canonical_title": "A Local Habitation",
        "format": "Hardcover",
        "isbn": "9780756405960",
        "owned_count": 1,
    }
    assert dict(dreamsnake) == {"status": "read", "date_read": "2020/01/02", "rating": 5.0}


def test_goodreads_ambiguous_matches_enter_review_queue(tmp_path: Path):
    database = Database(tmp_path / "ambiguous.sqlite3")
    database.initialize()
    database.upsert_calibre_book(
        "/library", {"id": "1", "title": "Foo-Bar", "authors": ["A. Writer"]}
    )
    database.upsert_calibre_book(
        "/library", {"id": "2", "title": "Foo Bar", "authors": ["A Writer"]}
    )
    export = tmp_path / "ambiguous.csv"
    export.write_text(CSV_HEADER + "12,Foo Bar,A Writer,,,0,Paperback,2020,,,to-read,1\n")

    summary = import_goodreads_csv(database, export)

    assert summary.ambiguous == 1
    with database.connect() as con:
        record = con.execute("SELECT work_id, match_status FROM source_records").fetchone()
        review = con.execute("SELECT kind, reason FROM review_queue").fetchone()
    assert record["work_id"] is None
    assert record["match_status"] == "AMBIGUOUS"
    assert review["kind"] == "SOURCE_MATCH"


def test_goodreads_matches_numbered_series_suffix_to_existing_work(tmp_path: Path):
    database = Database(tmp_path / "series-title.sqlite3")
    database.initialize()
    existing = database.upsert_calibre_book(
        "/library",
        {"id": "571", "title": "Rhythm of War", "authors": ["Brandon Sanderson"]},
    )
    assert existing.work_id is not None
    export = tmp_path / "series-title.csv"
    export.write_text(
        CSV_HEADER + '49021976,"Rhythm of War (The Stormlight Archive, #4)",Brandon Sanderson,,'
        '="9780765326386",4,Hardcover,2020,2026/09/08,,read,0\n'
    )

    summary = import_goodreads_csv(database, export)

    assert summary.matched == 1
    assert summary.created == 0
    with database.connect() as con:
        record = con.execute(
            "SELECT work_id, match_status, match_method FROM source_records"
        ).fetchone()
        work_count = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
        history = con.execute(
            """
            SELECT status, date_read FROM reading_status_observations
            WHERE work_id=?
            """,
            (existing.work_id,),
        ).fetchone()
    assert dict(record) == {
        "work_id": existing.work_id,
        "match_status": "MATCHED",
        "match_method": "title_author_without_series_suffix",
    }
    assert work_count == 1
    assert dict(history) == {"status": "read", "date_read": "2026/09/08"}


def test_newer_goodreads_export_preserves_earlier_observations(tmp_path: Path):
    database = Database(tmp_path / "history.sqlite3")
    database.initialize()
    first_export = tmp_path / "first.csv"
    first_export.write_text(
        CSV_HEADER + "10,Dreamsnake,Vonda N. McIntyre,,,5,Paperback,1978,2020/01/02,,read,0\n"
    )
    later_export = tmp_path / "later.csv"
    later_export.write_text(
        CSV_HEADER + "10,Dreamsnake,Vonda N. McIntyre,,,4,Paperback,1978,2020/01/02,,read,0\n"
    )

    import_goodreads_csv(database, first_export)
    import_goodreads_csv(database, later_export)

    with database.connect() as con:
        ratings = [
            row[0]
            for row in con.execute("SELECT rating FROM rating_observations ORDER BY id").fetchall()
        ]
        imports = con.execute("SELECT COUNT(*) FROM source_imports").fetchone()[0]
    assert ratings == [5.0, 4.0]
    assert imports == 2


def test_goodreads_cli_import_and_explain(tmp_path: Path):
    database_path = tmp_path / "cli.sqlite3"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"database: {database_path}\n")
    export = tmp_path / "goodreads.csv"
    _write_export(export)
    runner = CliRunner()

    imported = runner.invoke(
        cli_module.app,
        ["import-goodreads", str(export), "--config", str(config_path)],
    )
    explained = runner.invoke(
        cli_module.app,
        ["explain", "A Local Habitation", "--config", str(config_path)],
    )
    repeated = runner.invoke(
        cli_module.app,
        ["import-goodreads", str(export), "--config", str(config_path)],
    )

    assert imported.exit_code == 0, imported.output
    assert "2 Goodreads rows" in imported.output
    assert "2 works created" in imported.output
    assert explained.exit_code == 0, explained.output
    assert "Not scored yet" in explained.output
    assert "Personal history" in explained.output
    assert "status=unread" in explained.output
    assert "owned=1 Hardcover" in explained.output
    assert "goodreads tags: fantasy, urban-fantasy" in explained.output
    assert repeated.exit_code == 0, repeated.output
    assert "already imported" in repeated.output
