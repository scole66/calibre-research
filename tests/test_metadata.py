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

    provider = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact=None,
        max_retries=2,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )

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

    anonymous = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact=None,
        max_retries=2,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )
    identified = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact="reader@example.com",
        max_retries=2,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )

    assert anonymous.requests_per_second == 1.0
    assert identified.requests_per_second == 3.0
    assert "reader@example.com" in identified.user_agent


def test_openlibrary_rate_limiter_sleeps(monkeypatch):
    import calibre_research.providers as providers_module
    from calibre_research.providers import OpenLibraryProvider

    provider = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact=None,
        max_retries=2,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )
    provider._last_request_at = 10.0

    sleeps = []
    monkeypatch.setattr(providers_module.time, "monotonic", lambda: 10.25)
    monkeypatch.setattr(providers_module.time, "sleep", sleeps.append)

    provider._wait_for_rate_limit()

    assert sleeps == [0.75]


def test_openlibrary_isbn_uses_single_search_request(monkeypatch):
    from calibre_research.providers import JsonResponse, OpenLibraryProvider

    provider = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact=None,
        max_retries=2,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )
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


def test_classify_unresolved_unknown_author():
    from calibre_research.metadata import classify_unresolved

    assert (
        classify_unresolved({"title": "Player Core", "author": "Unknown"})
        == "MISSING_OR_SUSPICIOUS_AUTHOR"
    )


def test_classify_unresolved_generic():
    from calibre_research.metadata import classify_unresolved

    assert (
        classify_unresolved({"title": "A Conventional Boy", "author": "Charles Stross"})
        == "UNMATCHED_GENERIC"
    )


def test_googlebooks_selects_exact_match(monkeypatch):
    from calibre_research.providers import GoogleBooksProvider, JsonResponse

    provider = GoogleBooksProvider(
        base_url="https://example.invalid/books/v1",
        timeout=1.0,
        api_key=None,
        max_retries=0,
        requests_per_second=0,
    )

    def fake_get_json(url: str):
        return JsonResponse(
            url=url,
            data={
                "items": [
                    {
                        "volumeInfo": {
                            "title": "Dreamsnake Study Guide",
                            "authors": ["Someone Else"],
                            "publishedDate": "2020",
                        }
                    },
                    {
                        "volumeInfo": {
                            "title": "Dreamsnake",
                            "authors": ["Vonda N. McIntyre"],
                            "publisher": "Houghton Mifflin",
                            "publishedDate": "1978-03-29",
                            "language": "en",
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": "9781234567890"}
                            ],
                        }
                    },
                ]
            },
        )

    monkeypatch.setattr(provider, "_get_json", fake_get_json)
    candidate = provider.lookup(title="Dreamsnake", author="Vonda N. McIntyre")

    assert candidate is not None
    assert candidate.provider == "googlebooks"
    assert candidate.title == "Dreamsnake"
    assert candidate.publisher == "Houghton Mifflin"
    assert candidate.isbn == "9781234567890"
    assert candidate.original_publication_date is None
    assert candidate.edition_publication_date == "1978-03-29"
    assert candidate.identity_confidence == 0.95


def test_metadata_issue_can_be_resolved(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    result = db.upsert_calibre_book(
        "/library",
        {"id": "1", "title": "Player Core", "authors": ["Unknown"]},
    )
    assert result.ok
    edition = db.metadata_candidates(provider="openlibrary", limit=1)[0]

    db.record_metadata_issue(
        edition_id=edition["edition_id"],
        classification="MISSING_OR_SUSPICIOUS_AUTHOR",
        provider=None,
        reason="No configured provider produced a confident match",
    )
    with db.connect() as con:
        assert (
            con.execute("SELECT COUNT(*) FROM metadata_issues WHERE status='OPEN'").fetchone()[0]
            == 1
        )

    db.resolve_metadata_issues(edition_id=edition["edition_id"])
    with db.connect() as con:
        assert (
            con.execute("SELECT COUNT(*) FROM metadata_issues WHERE status='OPEN'").fetchone()[0]
            == 0
        )


def test_provider_factory_uses_config_values():
    from calibre_research.providers import GoogleBooksProvider, make_metadata_provider

    provider = make_metadata_provider(
        "googlebooks",
        {
            "base_url": "https://example.invalid/books/v1",
            "api_key": "secret",
            "timeout_seconds": 7.0,
            "max_retries": 4,
            "requests_per_second": 1.5,
        },
    )

    assert isinstance(provider, GoogleBooksProvider)
    assert provider.base_url == "https://example.invalid/books/v1"
    assert provider.api_key == "secret"
    assert provider.timeout == 7.0
    assert provider.max_retries == 4
    assert provider.requests_per_second == 1.5


def test_googlebooks_redacts_api_key_from_source_url(monkeypatch):
    from calibre_research.providers import GoogleBooksProvider, JsonResponse

    provider = GoogleBooksProvider(
        base_url="https://example.invalid/books/v1",
        timeout=1.0,
        api_key="top-secret",
        max_retries=0,
        requests_per_second=0,
    )

    def fake_get_json(url: str):
        return JsonResponse(
            url=url,
            data={
                "items": [
                    {
                        "volumeInfo": {
                            "title": "Dreamsnake",
                            "authors": ["Vonda N. McIntyre"],
                            "publishedDate": "1978",
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(provider, "_get_json", fake_get_json)
    candidate = provider.lookup(title="Dreamsnake", author="Vonda N. McIntyre")

    assert candidate is not None
    assert "top-secret" not in candidate.source_url
    assert "key=" not in candidate.source_url


def test_metadata_command_falls_back_in_configured_order(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    import calibre_research.cli as cli_module
    from calibre_research.metadata import MetadataCandidate

    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    db.upsert_calibre_book(
        "/library",
        {
            "id": "1",
            "title": "A Conventional Boy",
            "authors": ["Charles Stross"],
            "pubdate": "0101-01-01T00:00:00+00:00",
        },
    )

    calls = []

    class FakeProvider:
        def __init__(self, name):
            self.name = name

        def lookup(self, *, title, author, isbn=None):
            calls.append(self.name)
            if self.name == "openlibrary":
                return None
            return MetadataCandidate(
                provider="googlebooks",
                query_key="ignored",
                source_url="https://example.invalid/result",
                identity_confidence=0.95,
                title=title,
                authors=[author],
                edition_publication_date="2024",
            )

    monkeypatch.setattr(cli_module, "_db", lambda _path: db)
    monkeypatch.setattr(
        cli_module,
        "make_metadata_provider",
        lambda name, config: FakeProvider(name),
    )

    config = tmp_path / "config.yaml"
    config.write_text(
        f"""database: {tmp_path / "ignored.sqlite3"}\nmetadata:\n  providers:\n    - openlibrary\n    - googlebooks\n  reuse_cached_lookups: true\n  openlibrary:\n    base_url: https://openlibrary.org\n    contact: null\n    max_books_per_run: 25\n    timeout_seconds: 15\n    max_retries: 2\n    anonymous_requests_per_second: 1\n    identified_requests_per_second: 3\n  googlebooks:\n    base_url: https://www.googleapis.com/books/v1\n    api_key: null\n    timeout_seconds: 15\n    max_retries: 2\n    requests_per_second: 2\n"""
    )

    result = CliRunner().invoke(
        cli_module.app,
        ["metadata", "--limit", "1", "--config", str(config)],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["openlibrary", "googlebooks"]
    assert "googlebooks: confidence=0.95" in result.output


def test_http_provider_retries_retryable_error(monkeypatch):
    from calibre_research.providers import OpenLibraryProvider, RetryableProviderError

    provider = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact=None,
        max_retries=2,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )
    calls = []

    def fake_once(url: str):
        calls.append(url)
        if len(calls) < 3:
            raise RetryableProviderError("temporary")
        return None

    monkeypatch.setattr(provider, "_get_json_once", fake_once)
    # Remove Tenacity wait time from the unit test while leaving production
    # exponential+jitter behavior intact.
    monkeypatch.setattr(
        "calibre_research.providers.wait_random_exponential", lambda **kwargs: lambda retry_state: 0
    )

    # _get_json constructs its Retrying object at call time.
    assert provider._get_json("https://example.invalid") is None
    assert len(calls) == 3


def test_http_provider_stops_after_configured_retries(monkeypatch):
    from calibre_research.providers import OpenLibraryProvider, RetryableProviderError

    provider = OpenLibraryProvider(
        base_url="https://openlibrary.org",
        timeout=15.0,
        contact=None,
        max_retries=1,
        anonymous_requests_per_second=1.0,
        identified_requests_per_second=3.0,
    )
    calls = []

    def fake_once(url: str):
        calls.append(url)
        raise RetryableProviderError("temporary")

    monkeypatch.setattr(provider, "_get_json_once", fake_once)
    monkeypatch.setattr(
        "calibre_research.providers.wait_random_exponential", lambda **kwargs: lambda retry_state: 0
    )

    import pytest

    with pytest.raises(RetryableProviderError):
        provider._get_json("https://example.invalid")
    assert len(calls) == 2
