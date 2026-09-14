from pathlib import Path

from calibre_research.db import Database
from calibre_research.metadata import MetadataCandidate, metadata_query_key
from calibre_research.scoring import load_rubric, score_work
from calibre_research.significance import (
    CACHED_METADATA_CLAIM_CATEGORIES,
    CACHED_METADATA_FACT_FIELDS,
    build_about,
    build_why_read,
    claims_by_category,
    evidence_coverage,
    facts_by_name,
    research_from_cached_metadata,
)

RUBRIC_PATH = Path(__file__).parents[1] / "config" / "rubric-1.0.yaml"


def _cached_lookups() -> list[dict]:
    return [
        {
            "provider": "googlebooks",
            "source_url": "https://books.googleapis.com/books/v1/volumes/example",
            "identity_confidence": 0.95,
            "normalized": {"original_publication_date": None},
            "raw": {
                "selected": {
                    "volumeInfo": {
                        "description": "<b>A celebrated journey.</b> More publisher copy.",
                        "categories": ["Fiction", "Science Fiction"],
                        "averageRating": 4.2,
                        "ratingsCount": 120,
                    }
                }
            },
        },
        {
            "provider": "openlibrary",
            "source_url": "https://openlibrary.org/works/OL1W",
            "identity_confidence": 0.93,
            "normalized": {"original_publication_date": "1978"},
            "raw": {
                "selected": {
                    "subject": ["American Science Fiction", "Healing"],
                    "first_sentence": ["The desert smelled of dust and danger."],
                }
            },
        },
    ]


def test_cached_metadata_keeps_description_separate_from_why_read():
    rubric = load_rubric(RUBRIC_PATH)
    result = research_from_cached_metadata(
        title="Dreamsnake",
        author="Vonda N. McIntyre",
        lookups=_cached_lookups(),
    )
    facts = facts_by_name(result)

    total, components = score_work(
        rubric=rubric,
        claims_by_category=claims_by_category(result),
    )

    assert facts == {
        "google_description": "A celebrated journey. More publisher copy.",
        "google_categories": ["Fiction", "Science Fiction"],
        "google_average_rating": 4.2,
        "google_ratings_count": 120,
        "original_publication_year": 1978,
    }
    assert components["reader_reception"] == {
        "score": 0.0,
        "maximum": 10.0,
        "status": "unknown",
    }
    assert components["historical_interest"]["status"] == "unknown"
    assert components["personal_interest"]["status"] == "unknown"
    assert total == 0
    assert evidence_coverage(rubric=rubric, result=result, facts=facts) == 0
    assert build_about(facts=facts) == "A celebrated journey."
    assert build_why_read(claims=result.claims) == "No evidence-backed rationale available yet."
    assert result.estimated_cost_usd == 0


def test_sparse_evidence_reduces_coverage_not_worthiness():
    rubric = load_rubric(RUBRIC_PATH)
    result = research_from_cached_metadata(
        title="Obscure Book",
        author="Unknown Writer",
        lookups=[
            {
                "provider": "openlibrary",
                "source_url": "https://openlibrary.org/works/OL2W",
                "identity_confidence": 0.9,
                "normalized": {"original_publication_date": None},
                "raw": {"selected": {}},
            }
        ],
    )
    facts = facts_by_name(result)
    total, _ = score_work(rubric=rubric, claims_by_category={})

    assert total == 0
    assert evidence_coverage(rubric=rubric, result=result, facts=facts) == 0
    assert build_about(facts=facts) is None
    assert build_why_read(claims=result.claims) == "No evidence-backed rationale available yet."


def test_significance_cli_persists_evidence_and_ranks_work(tmp_path: Path):
    from typer.testing import CliRunner

    import calibre_research.cli as cli_module

    database_path = tmp_path / "test.sqlite3"
    db = Database(database_path)
    db.initialize()
    result = db.upsert_calibre_book(
        "/library",
        {
            "id": "1",
            "title": "Dreamsnake",
            "authors": ["Vonda N. McIntyre"],
            "pubdate": "2019-04-16",
        },
    )
    assert result.ok
    edition = db.metadata_candidates(providers=["googlebooks"])[0]
    query_key = metadata_query_key(
        title=edition["title"], author=edition["author"], isbn=edition["isbn"]
    )
    db.store_metadata_lookup(
        edition_id=edition["edition_id"],
        candidate=MetadataCandidate(
            provider="googlebooks",
            query_key=query_key,
            source_url="https://books.googleapis.com/books/v1/volumes/example",
            identity_confidence=0.95,
            title="Dreamsnake",
            authors=["Vonda N. McIntyre"],
            raw=_cached_lookups()[0]["raw"],
        ),
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""database: {database_path}
rubric: {RUBRIC_PATH}
"""
    )
    runner = CliRunner()

    research_result = runner.invoke(
        cli_module.app,
        ["research", "--depth", "significance", "--limit", "1", "--config", str(config_path)],
    )
    next_result = runner.invoke(
        cli_module.app,
        ["next", "--limit", "1", "--config", str(config_path)],
    )
    explain_result = runner.invoke(
        cli_module.app,
        ["explain", "Dreamsnake", "--config", str(config_path)],
    )

    assert research_result.exit_code == 0, research_result.output
    assert "cost=$0.00" in research_result.output
    assert "worthiness: not yet rated" in research_result.output
    assert "confidence: 0%" in research_result.output
    assert "why read: No evidence-backed rationale available yet." in research_result.output
    assert "about: A celebrated journey." in research_result.output
    assert next_result.exit_code == 0, next_result.output
    assert "No worthiness scores are available" in next_result.output
    assert explain_result.exit_code == 0, explain_result.output
    assert "Claims" not in explain_result.output
    assert "Sources (including unscored context)" in explain_result.output
    assert "books.googleapis.com" in explain_result.output

    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM significance_claims").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM fact_evidence").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == 0
        score = con.execute("SELECT total, confidence FROM scores").fetchone()
    assert score["total"] == 0
    assert score["confidence"] == 0

    assert db.significance_candidates(rubric_version="1.0") == []
    assert len(db.significance_candidates(rubric_version="1.0", refresh=True)) == 1

    with db.connect() as con:
        con.execute(
            """
            INSERT INTO facts(work_id, field_name, value_json, confidence)
            VALUES (?, 'manual_note', 'true', 1.0)
            """,
            (result.work_id,),
        )
        con.execute(
            """
            INSERT INTO significance_claims(work_id, category, claim, confidence)
            VALUES (?, 'critical_reputation', 'Manually researched claim.', 1.0)
            """,
            (result.work_id,),
        )
    refreshed_result = research_from_cached_metadata(
        title="Dreamsnake",
        author="Vonda N. McIntyre",
        lookups=[
            {
                "provider": "openlibrary",
                "source_url": "https://openlibrary.org/works/OL1W",
                "identity_confidence": 0.93,
                "normalized": {"original_publication_date": None},
                "raw": {"selected": {}},
            }
        ],
    )
    assert result.work_id is not None
    db.store_research_result(
        work_id=result.work_id,
        result=refreshed_result,
        managed_fact_fields=CACHED_METADATA_FACT_FIELDS,
        managed_claim_categories=CACHED_METADATA_CLAIM_CATEGORIES,
    )
    with db.connect() as con:
        assert (
            con.execute("SELECT COUNT(*) FROM facts WHERE status='RESEARCHED'").fetchone()[0] == 1
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM significance_claims WHERE status='RESEARCHED'"
            ).fetchone()[0]
            == 1
        )
        assert con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
