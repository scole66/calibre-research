import io
import json
from urllib.parse import parse_qs, urlsplit

from calibre_research.config import WikidataConfig
from calibre_research.scoring import assessed_maximum, score_work
from calibre_research.significance import build_why_read, claims_by_category, evidence_coverage
from calibre_research.wikidata import WikidataAwardProvider


def _entity(label, claims=None):
    return {"labels": {"en": {"value": label}}, "claims": claims or {}}


def _claim(entity_id):
    return {"mainsnak": {"datavalue": {"value": {"id": entity_id}}}}


def test_wikidata_awards_require_matching_title_and_author(monkeypatch):
    responses = {
        "wbsearchentities": {"search": [{"id": "QWORK"}]},
        "works": {
            "entities": {
                "QWORK": _entity(
                    "Dreamsnake",
                    {"P50": [_claim("QAUTHOR")], "P166": [_claim("QHUGO")]},
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
    result = WikidataAwardProvider(WikidataConfig(enabled=True)).research(
        title="Dreamsnake: A Novel", author="Vonda N. McIntyre"
    )

    assert [claim.claim for claim in result.claims] == ["Won the Hugo Award for Best Novel."]
    assert str(result.claims[0].evidence[0].source_url) == "https://www.wikidata.org/wiki/QWORK"
    rubric = {
        "components": {
            "major_awards": {"max": 25, "rules": {"win": 8, "nomination": 3}},
            "personal_interest": {"max": 15},
        }
    }
    total, components = score_work(rubric=rubric, claims_by_category=claims_by_category(result))
    assert total == 8
    assert assessed_maximum(components) == 25
    assert evidence_coverage(rubric=rubric, result=result, facts={}) == 0.594
    assert build_why_read(claims=result.claims) == "Won the Hugo Award for Best Novel."


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
    result = WikidataAwardProvider(WikidataConfig(enabled=True)).research(
        title="Shared Title", author="Right Author"
    )
    assert result.identity_confidence == 0
    assert result.claims == []
