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

    edition = db.metadata_candidates(providers=["openlibrary"], limit=1)[0]
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
    edition = db.metadata_candidates(providers=["openlibrary"], limit=1)[0]

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


def test_metadata_queue_separates_pending_completed_and_provider_errors(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    for book_id, title in enumerate(
        ["Pending", "Partial", "Unmatched", "Matched", "Provider Error"], start=1
    ):
        result = db.upsert_calibre_book(
            "/library",
            {"id": str(book_id), "title": title, "authors": ["Test Author"]},
        )
        assert result.ok

    editions = {
        row["title"]: row
        for row in db.metadata_candidates(providers=["openlibrary", "googlebooks"])
    }
    for provider_name in ["openlibrary"]:
        db.store_metadata_miss(
            edition_id=editions["Partial"]["edition_id"],
            provider=provider_name,
            query_key=metadata_query_key(title="Partial", author="Test Author", isbn=None),
        )
    for provider_name in ["openlibrary", "googlebooks"]:
        db.store_metadata_miss(
            edition_id=editions["Unmatched"]["edition_id"],
            provider=provider_name,
            query_key=metadata_query_key(title="Unmatched", author="Test Author", isbn=None),
        )
    db.store_metadata_lookup(
        edition_id=editions["Matched"]["edition_id"],
        candidate=MetadataCandidate(
            provider="openlibrary",
            query_key=metadata_query_key(title="Matched", author="Test Author", isbn=None),
            source_url="https://example.invalid/match",
            identity_confidence=0.95,
            title="Matched",
            authors=["Test Author"],
        ),
    )
    db.record_metadata_issue(
        edition_id=editions["Provider Error"]["edition_id"],
        classification="PROVIDER_ERROR",
        provider="googlebooks",
        reason="temporary failure",
    )

    pending = db.metadata_candidates(providers=["openlibrary", "googlebooks"])
    retryable = db.metadata_candidates(providers=["openlibrary", "googlebooks"], retry_errors=True)
    pending_without_failed_provider = db.metadata_candidates(providers=["openlibrary"])
    refreshed = db.metadata_candidates(providers=["openlibrary", "googlebooks"], refresh=True)

    assert {row["title"] for row in pending} == {"Pending", "Partial"}
    assert [row["title"] for row in retryable] == ["Provider Error"]
    assert "Provider Error" in {row["title"] for row in pending_without_failed_provider}
    assert {row["title"] for row in refreshed} == {
        "Pending",
        "Partial",
        "Unmatched",
        "Matched",
        "Provider Error",
    }
    assert db.open_metadata_error_providers(
        edition_id=editions["Provider Error"]["edition_id"]
    ) == {"googlebooks"}

    for book_id, title in [(3, "Unmatched Revised"), (4, "Matched Revised")]:
        result = db.upsert_calibre_book(
            "/library",
            {"id": str(book_id), "title": title, "authors": ["Test Author"]},
        )
        assert result.ok

    pending_after_rescan = db.metadata_candidates(providers=["openlibrary", "googlebooks"])
    assert {row["title"] for row in pending_after_rescan} == {
        "Pending",
        "Partial",
        "Unmatched Revised",
        "Matched Revised",
    }


def test_metadata_issue_can_be_resolved_after_being_reopened(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    result = db.upsert_calibre_book(
        "/library", {"id": "1", "title": "Dreamsnake", "authors": ["Vonda N. McIntyre"]}
    )
    assert result.ok
    edition = db.metadata_candidates(providers=["openlibrary"])[0]

    for _ in range(2):
        db.record_metadata_issue(
            edition_id=edition["edition_id"],
            classification="PROVIDER_ERROR",
            provider="openlibrary",
            reason="temporary failure",
        )
        db.resolve_metadata_issues(edition_id=edition["edition_id"])

    with db.connect() as con:
        rows = con.execute(
            "SELECT status FROM metadata_issues WHERE edition_id=?",
            (edition["edition_id"],),
        ).fetchall()
    assert [row["status"] for row in rows] == ["RESOLVED"]


def test_provider_factory_uses_config_values():
    from calibre_research.providers import GoogleBooksProvider, make_metadata_provider

    provider = make_metadata_provider(
        "googlebooks",
        {
            "base_url": "https://example.invalid/books/v1",
            "api_key": "secret",
            "timeout_seconds": 7.0,
            "max_retries": 4,
            "retry_wait_multiplier_seconds": 0.75,
            "retry_wait_max_seconds": 20.0,
            "requests_per_second": 1.5,
        },
    )

    assert isinstance(provider, GoogleBooksProvider)
    assert provider.base_url == "https://example.invalid/books/v1"
    assert provider.api_key == "secret"
    assert provider.timeout == 7.0
    assert provider.max_retries == 4
    assert provider.retry_wait_multiplier_seconds == 0.75
    assert provider.retry_wait_max_seconds == 20.0
    assert provider.requests_per_second == 1.5


def test_googlebooks_api_key_command_takes_precedence(monkeypatch):
    import subprocess

    from calibre_research.providers import GoogleBooksProvider, make_metadata_provider

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=" command-secret\n", stderr="")

    monkeypatch.setattr("calibre_research.providers.subprocess.run", fake_run)
    provider = make_metadata_provider(
        "googlebooks",
        {
            "base_url": "https://example.invalid/books/v1",
            "api_key": "literal-secret",
            "api_key_command": ["op", "read", "op://vault/item/field"],
            "api_key_command_timeout_seconds": 9.0,
            "timeout_seconds": 7.0,
            "max_retries": 4,
            "retry_wait_multiplier_seconds": 0.5,
            "retry_wait_max_seconds": 12.0,
            "requests_per_second": 1.5,
        },
    )

    assert isinstance(provider, GoogleBooksProvider)
    assert provider.api_key == "command-secret"
    assert calls == [
        (
            ["op", "read", "op://vault/item/field"],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 9.0,
            },
        )
    ]
    assert provider.retry_wait_multiplier_seconds == 0.5
    assert provider.retry_wait_max_seconds == 12.0


def test_googlebooks_api_key_command_has_no_default_timeout(tmp_path, monkeypatch):
    import subprocess

    from calibre_research.config import load_config
    from calibre_research.providers import make_metadata_provider

    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="secret\n", stderr="")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """metadata:
  googlebooks:
    base_url: https://example.invalid/books/v1
    api_key_command: [op, read, op://vault/item/field]
    timeout_seconds: 7
    max_retries: 1
    requests_per_second: 1
"""
    )
    config = load_config(config_path)
    monkeypatch.setattr("calibre_research.providers.subprocess.run", fake_run)

    assert config.metadata.googlebooks is not None
    make_metadata_provider("googlebooks", config.metadata.googlebooks.model_dump())

    assert calls[0]["timeout"] is None


def test_googlebooks_requires_credentials_when_enabled():
    import pytest

    from calibre_research.providers import ProviderConfigurationError, make_metadata_provider

    with pytest.raises(ProviderConfigurationError, match="no credentials"):
        make_metadata_provider(
            "googlebooks",
            {
                "base_url": "https://example.invalid/books/v1",
                "api_key": None,
                "api_key_command": None,
                "api_key_command_timeout_seconds": 15.0,
                "timeout_seconds": 7.0,
                "max_retries": 1,
                "retry_wait_multiplier_seconds": 1.0,
                "retry_wait_max_seconds": 30.0,
                "requests_per_second": 1.5,
            },
        )


def test_metadata_command_reports_missing_googlebooks_credentials(tmp_path):
    from typer.testing import CliRunner

    import calibre_research.cli as cli_module

    config = tmp_path / "config.yaml"
    config.write_text(
        f"""database: {tmp_path / "test.sqlite3"}
metadata:
  providers: [googlebooks]
  googlebooks:
    base_url: https://www.googleapis.com/books/v1
    timeout_seconds: 15
    max_retries: 2
    requests_per_second: 2
"""
    )

    result = CliRunner().invoke(cli_module.app, ["metadata", "--config", str(config)])

    assert result.exit_code == 1
    assert "googlebooks is enabled but no credentials are configured" in result.output


def test_googlebooks_rejects_empty_api_key_command():
    import pytest

    from calibre_research.providers import ProviderConfigurationError, resolve_api_key

    with pytest.raises(ProviderConfigurationError, match="non-empty list"):
        resolve_api_key({"api_key_command": []})


def test_googlebooks_key_command_failure_surfaces_stderr_but_not_stdout(monkeypatch):
    import subprocess

    import pytest

    from calibre_research.providers import ProviderConfigurationError, resolve_api_key

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="secret-from-stdout", stderr="secret-from-stderr"
        )

    monkeypatch.setattr("calibre_research.providers.subprocess.run", fake_run)
    with pytest.raises(ProviderConfigurationError) as caught:
        resolve_api_key(
            {
                "api_key_command": ["op", "read", "op://vault/item/field"],
                "api_key_command_timeout_seconds": 15.0,
            }
        )

    message = str(caught.value)
    assert "secret-from-stdout" not in message
    assert message.endswith(": secret-from-stderr")


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


def test_googlebooks_zero_daily_quota_disables_provider_without_retry(monkeypatch):
    import io
    from email.message import Message
    from urllib.error import HTTPError

    import pytest

    from calibre_research.providers import GoogleBooksProvider, ProviderUnavailableError

    provider = GoogleBooksProvider(
        base_url="https://example.invalid/books/v1",
        timeout=1.0,
        api_key="top-secret",
        max_retries=3,
        requests_per_second=0,
    )
    body = b"""{
      "error": {
        "code": 429,
        "message": "Quota exceeded for quota metric 'Queries' and limit 'Queries per day'",
        "details": [{"metadata": {"quota_limit_value": "0"}}]
      }
    }"""
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            Message(),
            io.BytesIO(body),
        )

    monkeypatch.setattr("calibre_research.providers.urlopen", fake_urlopen)

    with pytest.raises(ProviderUnavailableError, match="disabled for the remainder"):
        provider.lookup(title="Dreamsnake", author="Vonda N. McIntyre")
    with pytest.raises(ProviderUnavailableError, match="disabled for the remainder"):
        provider.lookup(title="Another Book", author="Another Author")

    assert len(calls) == 1


def test_googlebooks_ordinary_429_remains_retryable():
    from calibre_research.providers import GoogleBooksProvider, RetryableProviderError

    provider = GoogleBooksProvider(
        base_url="https://example.invalid/books/v1",
        timeout=1.0,
        api_key="top-secret",
        max_retries=1,
        requests_per_second=0,
    )

    error = provider._http_error(
        429,
        "https://example.invalid/books/v1/volumes?key=top-secret",
        '{"error":{"message":"key top-secret","errors":[{"reason":"rateLimitExceeded"}]}}',
    )

    assert isinstance(error, RetryableProviderError)
    assert "top-secret" not in str(error)


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
        f"""database: {tmp_path / "ignored.sqlite3"}
metadata:
  providers:
    - openlibrary
    - googlebooks
  reuse_cached_lookups: true
  openlibrary:
    base_url: https://openlibrary.org
    contact: null
    max_books_per_run: 25
    timeout_seconds: 15
    max_retries: 2
    anonymous_requests_per_second: 1
    identified_requests_per_second: 3
  googlebooks:
    base_url: https://www.googleapis.com/books/v1
    api_key: null
    timeout_seconds: 15
    max_retries: 2
    requests_per_second: 2
"""
    )

    result = CliRunner().invoke(
        cli_module.app,
        ["metadata", "--limit", "1", "--config", str(config)],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["openlibrary", "googlebooks"]
    assert "googlebooks: confidence=0.95" in result.output


def test_metadata_retry_errors_retries_only_failed_provider_and_reclassifies(
    tmp_path: Path, monkeypatch
):
    from typer.testing import CliRunner

    import calibre_research.cli as cli_module

    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    result = db.upsert_calibre_book(
        "/library",
        {
            "id": "1",
            "title": "A Conventional Boy",
            "authors": ["Charles Stross"],
        },
    )
    assert result.ok
    edition = db.metadata_candidates(providers=["openlibrary", "googlebooks"])[0]
    query_key = metadata_query_key(
        title=edition["title"], author=edition["author"], isbn=edition["isbn"]
    )
    db.store_metadata_miss(
        edition_id=edition["edition_id"],
        provider="openlibrary",
        query_key=query_key,
    )
    db.record_metadata_issue(
        edition_id=edition["edition_id"],
        classification="PROVIDER_ERROR",
        provider="googlebooks",
        reason="temporary failure",
    )
    calls = []

    class FakeProvider:
        def __init__(self, name):
            self.name = name

        def lookup(self, *, title, author, isbn=None):
            calls.append(self.name)
            return None

    monkeypatch.setattr(cli_module, "_db", lambda _path: db)
    monkeypatch.setattr(
        cli_module,
        "make_metadata_provider",
        lambda name, config: FakeProvider(name),
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""database: {tmp_path / "ignored.sqlite3"}
metadata:
  providers: [openlibrary, googlebooks]
  reuse_cached_lookups: true
  openlibrary:
    base_url: https://openlibrary.org
    max_books_per_run: 25
    timeout_seconds: 15
    max_retries: 2
    anonymous_requests_per_second: 1
    identified_requests_per_second: 3
  googlebooks:
    base_url: https://www.googleapis.com/books/v1
    timeout_seconds: 15
    max_retries: 2
    requests_per_second: 2
"""
    )
    runner = CliRunner()

    retry_result = runner.invoke(
        cli_module.app,
        ["metadata", "--retry-errors", "--config", str(config)],
    )

    assert retry_result.exit_code == 0, retry_result.output
    assert calls == ["googlebooks"]
    assert "No confident metadata match" in retry_result.output
    with db.connect() as con:
        open_issues = con.execute(
            """
            SELECT classification, provider
            FROM metadata_issues
            WHERE edition_id=? AND status='OPEN'
            """,
            (edition["edition_id"],),
        ).fetchall()
    assert [(row["classification"], row["provider"]) for row in open_issues] == [
        ("UNMATCHED_GENERIC", "")
    ]

    second_retry = runner.invoke(
        cli_module.app,
        ["metadata", "--retry-errors", "--config", str(config)],
    )
    normal_run = runner.invoke(cli_module.app, ["metadata", "--config", str(config)])

    assert second_retry.exit_code == 0
    assert "No editions with provider errors to retry" in second_retry.output
    assert normal_run.exit_code == 0
    assert "No pending editions to research" in normal_run.output
    assert calls == ["googlebooks"]


def test_metadata_retry_errors_rejects_refresh(tmp_path: Path):
    from typer.testing import CliRunner

    import calibre_research.cli as cli_module

    config = tmp_path / "config.yaml"
    config.write_text(
        f"""database: {tmp_path / "test.sqlite3"}
metadata:
  providers: [openlibrary]
  openlibrary:
    base_url: https://openlibrary.org
    max_books_per_run: 25
    timeout_seconds: 15
    max_retries: 2
    anonymous_requests_per_second: 1
    identified_requests_per_second: 3
"""
    )

    result = CliRunner().invoke(
        cli_module.app,
        ["metadata", "--retry-errors", "--refresh", "--config", str(config)],
    )

    assert result.exit_code == 1
    assert "--retry-errors cannot be combined with --refresh" in result.output


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
