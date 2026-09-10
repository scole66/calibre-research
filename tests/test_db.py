from pathlib import Path
from calibre_research.db import Database


def test_initialize_and_upsert(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    db.upsert_calibre_book(
        "/library",
        {
            "id": "1",
            "title": "Dreamsnake",
            "authors": "Vonda N. McIntyre",
            "pubdate": "2019-04-16",
        },
    )
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1
