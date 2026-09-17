import io
import json
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from calibre_research.config import WikidataConfig
from calibre_research.db import Database
from calibre_research.scoring import assessed_maximum, score_significance
from calibre_research.significance import build_significance_explanation, evidence_coverage
from calibre_research.wikidata import WikidataAwardProvider


def _entity(label, claims=None):
    return {"labels": {"en": {"value": label}}, "claims": claims or {}}


def _claim(entity_id, *, statement_id=None, year=None):
    claim = {
        "mainsnak": {"datavalue": {"value": {"id": entity_id}}},
        "id": statement_id,
    }
    if year:
        claim["qualifiers"] = {
            "P585": [{"datavalue": {"value": {"time": f"+{year}-00-00T00:00:00Z"}}}]
        }
    return claim


def test_wikidata_awards_require_matching_title_and_author(monkeypatch, tmp_path):
    responses = {
        "wbsearchentities": {"search": [{"id": "QWORK"}]},
        "works": {
            "entities": {
                "QWORK": _entity(
                    "Dreamsnake",
                    {
                        "P50": [_claim("QAUTHOR")],
                        "P166": [_claim("QHUGO", statement_id="QWORK$award", year=1979)],
                    },
                )
            }
        },
        "authors": {"entities": {"QAUTHOR": _entity("Vonda N. McIntyre")}},
        "awards": {"entities": {"QHUGO": _entity("Hugo Award for Best Novel")}},
    }

    def fake_urlopen(request, timeout):
        params = parse_qs(urlsplit(request.full_url).query)
        action = params["action"][0]
        if action == "wbsearchentities":
            payload = responses[action]
        else:
            ids = params["ids"][0]
            key = {"QWORK": "works", "QAUTHOR": "authors", "QHUGO": "awards"}[ids]
            payload = responses[key]
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("calibre_research.wikidata.urlopen", fake_urlopen)
    result = WikidataAwardProvider(WikidataConfig(enabled=True, requests_per_second=0)).research(
        title="Dreamsnake: A Novel", author="Vonda N. McIntyre"
    )

    assert result.claims == []
    assert result.awards[0].model_dump(mode="json") == {
        "award_name": "Hugo Award for Best Novel",
        "year": 1979,
        "category": None,
        "result": "winner",
        "source_name": "Wikidata",
        "source_identifier": "QWORK$award",
        "source_url": "https://www.wikidata.org/wiki/QWORK",
        "confidence": 0.95,
    }
    rubric = {
        "components": {
            "major_awards": {"max": 25, "rules": {"win": 8, "nomination": 3}},
            "personal_interest": {"max": 15},
        }
    }
    total, components = score_significance(rubric=rubric, awards=result.awards)
    assert total == 8
    assert assessed_maximum(components) == 25
    assert evidence_coverage(rubric=rubric, result=result, facts={}) == 0.594
    assert build_significance_explanation(awards=result.awards) == (
        "Won the Hugo Award for Best Novel (1979)."
    )

    db = Database(tmp_path / "awards.sqlite3")
    db.initialize()
    work = db.upsert_calibre_book(
        "/library",
        {"id": "1", "title": "Dreamsnake", "authors": ["Vonda N. McIntyre"]},
    )
    assert work.work_id is not None
    db.store_research_result(
        work_id=work.work_id,
        result=result,
        managed_award_sources={"Wikidata"},
    )
    db.store_research_result(
        work_id=work.work_id,
        result=result,
        managed_award_sources={"Wikidata"},
    )
    with db.connect() as con:
        award = con.execute("SELECT * FROM award_evidence").fetchone()
        assert con.execute("SELECT COUNT(*) FROM award_evidence").fetchone()[0] == 1
    assert award["award_name"] == "Hugo Award for Best Novel"
    assert award["award_year"] == 1979
    assert award["result"] == "winner"
    assert award["source_identifier"] == "QWORK$award"
    stored_awards = db.award_evidence_for_work(work_id=work.work_id)
    stored_total, _ = score_significance(rubric=rubric, awards=stored_awards)
    assert stored_total == 8


def test_wikidata_rejects_same_title_by_another_author(monkeypatch):
    payloads = iter(
        [
            {"search": [{"id": "QWORK"}]},
            {"entities": {"QWORK": _entity("Shared Title", {"P50": [_claim("QAUTHOR")]})}},
            {"entities": {"QAUTHOR": _entity("Someone Else")}},
        ]
    )

    monkeypatch.setattr(
        "calibre_research.wikidata.urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(next(payloads)).encode()),
    )
    result = WikidataAwardProvider(WikidataConfig(enabled=True, requests_per_second=0)).research(
        title="Shared Title", author="Right Author"
    )
    assert result.identity_confidence == 0
    assert result.awards == []


def test_wikidata_identifies_paces_and_honors_retry_after(monkeypatch):
    calls = []
    waits = []
    headers = Message()
    headers["Retry-After"] = "7"

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "Too Many Requests", headers, None)
        return io.BytesIO(json.dumps({"search": []}).encode())

    monkeypatch.setattr("calibre_research.wikidata.urlopen", fake_urlopen)
    monkeypatch.setattr("calibre_research.wikidata.time.sleep", waits.append)
    provider = WikidataAwardProvider(
        WikidataConfig(
            enabled=True,
            contact="mailto:maintainer@example.com",
            requests_per_second=0,
            max_retries=1,
        )
    )

    result = provider.research(title="Book", author="Author")

    assert result.awards == []
    assert waits == [7.0]
    assert "maxlag=5" in calls[0].full_url
    assert calls[0].get_header("User-agent") == (
        "calibre-research-bot/0.1 (mailto:maintainer@example.com)"
    )
