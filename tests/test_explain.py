from pathlib import Path

from typer.testing import CliRunner

import calibre_research.cli as cli_module
from calibre_research.db import Database


def test_explain_prefers_exact_title_and_refuses_ambiguous_partial_match(tmp_path: Path):
    database_path = tmp_path / "explain.sqlite3"
    database = Database(database_path)
    database.initialize()
    database.upsert_calibre_book(
        "/library", {"id": "1", "title": "Children of Dune", "authors": ["Frank Herbert"]}
    )
    database.upsert_calibre_book(
        "/library", {"id": "2", "title": "God Emperor of Dune", "authors": ["Frank Herbert"]}
    )
    database.upsert_calibre_book(
        "/library", {"id": "3", "title": "Dune", "authors": ["Frank Herbert"]}
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"database: {database_path}\n")
    runner = CliRunner()

    exact = runner.invoke(cli_module.app, ["explain", "dUnE", "--config", str(config_path)])
    unique_partial = runner.invoke(
        cli_module.app, ["explain", "Children", "--config", str(config_path)]
    )
    ambiguous = runner.invoke(cli_module.app, ["explain", "of Dune", "--config", str(config_path)])

    assert exact.exit_code == 0, exact.output
    assert "Dune — Frank Herbert" in exact.output
    assert "Children of Dune" not in exact.output
    assert unique_partial.exit_code == 0, unique_partial.output
    assert "Children of Dune — Frank Herbert" in unique_partial.output
    assert ambiguous.exit_code == 1
    assert "Multiple works match 'of Dune'" in ambiguous.output
    assert "Children of Dune — Frank Herbert" in ambiguous.output
    assert "God Emperor of Dune — Frank Herbert" in ambiguous.output
    assert "Use a more specific title." in ambiguous.output
