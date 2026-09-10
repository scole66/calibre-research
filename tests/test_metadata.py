from pathlib import Path

from calibre_research.db import Database
from calibre_research.metadata import MetadataCandidate, build_proposals
from calibre_research.providers import metadata_query_key


def test_build_proposals_only_fills_missing_fields():
    edition = {
        "title": "Dreamsnake",
        "author": "Vonda N. McIntyre",
        "isbn": None,
        "publisher": "Existing Publisher",
        "publication_date": "0101-01-01T00:00:00+00:00",
        "language": None,
        "series": None,
        "series_index": 1.0,
    }
    candidate = MetadataCandidate(
        provider="openlibrary",
        query_key="title-author:dreamsnake|vonda n mcintyre",
        source_url="https://openlibrary.org/search.json",
        identity_confidence=0.93,
        title="Dreamsnake",
        authors=["Vonda N. McIntyre"],
        isbn="9781234567890",
        publisher="Different Publisher",
        original_publication_date="1978",
        language="eng",
    )

    proposals = build_proposals(edition, candidate)
    by_field = {proposal.field_name: proposal for proposal in proposals}

    assert by_field["isbn"].proposed_value == "9781234567890"
    assert by_field["publication_date"].proposed_value == "1978"
    assert by_field["language"].proposed_value == "eng"
    assert "publisher" not in by_field
    assert "series_index" not in by_field


def test_metadata_lookup_and_proposals_are_cached(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    result = db.upsert_calibre_book(
        "/library",
        {
            "id": "1",
            "title": "Dreamsnake",
            "authors": ["Vonda N. McIntyre"],
            "pubdate": "0101-01-01T00:00:00+00:00",
            "languages": ["eng"],
        },
    )
    assert result.ok

    edition = db.metadata_candidates(provider="openlibrary", limit=1)[0]
    candidate = MetadataCandidate(
        provider="openlibrary",
        query_key="title-author:dreamsnake|vonda n mcintyre",
        source_url="https://openlibrary.org/search.json",
        identity_confidence=0.93,
        title="Dreamsnake",
        authors=["Vonda N. McIntyre"],
        isbn="9781234567890",
        original_publication_date="1978",
        raw={"docs": [{"title": "Dreamsnake"}]},
    )

    lookup_id = db.store_metadata_lookup(edition_id=edition["edition_id"], candidate=candidate)
    proposals = build_proposals(edition, candidate)
    db.replace_metadata_proposals(
        edition_id=edition["edition_id"], lookup_id=lookup_id, proposals=proposals
    )

    cached = db.cached_metadata_lookup(
        edition_id=edition["edition_id"],
        provider="openlibrary",
        query_key=candidate.query_key,
    )
    assert cached is not None
    assert cached["normalized"]["original_publication_date"] == "1978"

    with db.connect() as con:
        fields = {
            row[0]
            for row in con.execute(
                "SELECT field_name FROM metadata_proposals WHERE edition_id=?",
                (edition["edition_id"],),
            ).fetchall()
        }
    assert "isbn" in fields
    assert "publication_date" in fields


def test_metadata_query_key_is_stable_with_isbn_fallback():
    key = metadata_query_key(
        title="Dreamsnake", author="Vonda N. McIntyre", isbn="978-1-234-56789-0"
    )
    assert key == "isbn:9781234567890|title-author:dreamsnake|vonda n mcintyre"


def test_openlibrary_title_author_search_selects_exact_match(monkeypatch):
    from calibre_research.providers import JsonResponse, OpenLibraryProvider

    provider = OpenLibraryProvider()

    def fake_get_json(url: str):
        return JsonResponse(
            url=url,
            data={
                "docs": [
                    {
                        "title": "Dreamsnake Study Guide",
                        "author_name": ["Someone Else"],
                        "first_publish_year": 2020,
                    },
                    {
                        "title": "Dreamsnake",
                        "author_name": ["Vonda N. McIntyre"],
                        "first_publish_year": 1978,
                        "isbn": ["9781234567890"],
                        "publisher": ["Houghton Mifflin"],
                        "language": ["eng"],
                    },
                ]
            },
        )

    monkeypatch.setattr(provider, "_get_json", fake_get_json)
    candidate = provider.lookup(title="Dreamsnake", author="Vonda N. McIntyre")

    assert candidate is not None
    assert candidate.title == "Dreamsnake"
    assert candidate.original_publication_date == "1978"
    assert candidate.publisher == "Houghton Mifflin"
    assert candidate.identity_confidence == 0.95


def test_openlibrary_identification_controls_request_rate():
    from calibre_research.providers import OpenLibraryProvider

    anonymous = OpenLibraryProvider()
    identified = OpenLibraryProvider(contact="reader@example.com")

    assert anonymous.requests_per_second == 1.0
    assert identified.requests_per_second == 3.0
    assert "reader@example.com" in identified.user_agent


def test_openlibrary_rate_limiter_sleeps(monkeypatch):
    import calibre_research.providers as providers_module
    from calibre_research.providers import OpenLibraryProvider

    provider = OpenLibraryProvider()
    provider._last_request_at = 10.0

    sleeps = []
    monkeypatch.setattr(providers_module.time, "monotonic", lambda: 10.25)
    monkeypatch.setattr(providers_module.time, "sleep", sleeps.append)

    provider._wait_for_rate_limit()

    assert sleeps == [0.75]


def test_openlibrary_isbn_uses_single_search_request(monkeypatch):
    from calibre_research.providers import JsonResponse, OpenLibraryProvider

    provider = OpenLibraryProvider()
    urls = []

    def fake_get_json(url: str):
        urls.append(url)
        return JsonResponse(
            url=url,
            data={
                "docs": [
                    {
                        "title": "Dreamsnake",
                        "author_name": ["Vonda N. McIntyre"],
                        "first_publish_year": 1978,
                        "isbn": ["9781234567890"],
                        "publisher": ["Houghton Mifflin"],
                        "language": ["eng"],
                        "publish_date": ["1978"],
                    }
                ]
            },
        )

    monkeypatch.setattr(provider, "_get_json", fake_get_json)
    candidate = provider.lookup(
        title="Dreamsnake",
        author="Vonda N. McIntyre",
        isbn="9781234567890",
    )

    assert candidate is not None
    assert candidate.identity_confidence == 0.99
    assert len(urls) == 1
    assert "search.json" in urls[0]
    assert "isbn=9781234567890" in urls[0]
