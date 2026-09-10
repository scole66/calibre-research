from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .metadata import MetadataCandidate, normalize_text
from .models import ResearchResult

USER_AGENT_BASE = "calibre-research/0.1 (+https://github.com/scole66/calibre-research)"


class ProviderError(RuntimeError):
    pass


class ResearchProvider(ABC):
    @abstractmethod
    def research(self, *, title: str, author: str, depth: str) -> ResearchResult:
        raise NotImplementedError


class StubResearchProvider(ResearchProvider):
    def research(self, *, title: str, author: str, depth: str) -> ResearchResult:
        return ResearchResult(
            title=title,
            author=author,
            identity_confidence=1.0,
            facts=[],
            claims=[],
            estimated_cost_usd=0.0,
        )


class MetadataProvider(ABC):
    name: str

    @abstractmethod
    def lookup(
        self,
        *,
        title: str,
        author: str,
        isbn: str | None = None,
    ) -> MetadataCandidate | None:
        raise NotImplementedError


@dataclass(frozen=True)
class JsonResponse:
    url: str
    data: dict[str, Any]


class OpenLibraryProvider(MetadataProvider):
    name = "openlibrary"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        contact: str | None = None,
        max_retries: int = 2,
    ):
        self.timeout = timeout
        self.contact = contact
        self.max_retries = max_retries
        self.requests_per_second = 3.0 if contact else 1.0
        self._last_request_at: float | None = None

    @property
    def user_agent(self) -> str:
        if self.contact:
            return (
                "calibre-research/0.1 "
                f"({self.contact}; +https://github.com/scole66/calibre-research)"
            )
        return USER_AGENT_BASE

    def lookup(
        self,
        *,
        title: str,
        author: str,
        isbn: str | None = None,
    ) -> MetadataCandidate | None:
        query_key = metadata_query_key(title=title, author=author, isbn=isbn)
        clean_isbn = _clean_isbn(isbn)
        if clean_isbn:
            candidate = self._lookup_isbn(clean_isbn)
            if candidate is not None:
                return replace(candidate, query_key=query_key)
        candidate = self._search(title=title, author=author)
        if candidate is not None:
            return replace(candidate, query_key=query_key)
        return None

    def _lookup_isbn(self, isbn: str) -> MetadataCandidate | None:
        params = urlencode(
            {
                "isbn": isbn,
                "limit": 1,
                "fields": (
                    "key,title,author_name,first_publish_year,isbn,publisher,language,"
                    "publish_date,edition_key"
                ),
            }
        )
        response = self._get_json(f"https://openlibrary.org/search.json?{params}")
        if response is None:
            return None

        docs = response.data.get("docs") or []
        if not docs:
            return None

        best = docs[0]
        first_year = best.get("first_publish_year")
        original_date = str(first_year) if isinstance(first_year, int) else None

        return MetadataCandidate(
            provider=self.name,
            query_key=f"isbn:{isbn}",
            source_url=response.url,
            identity_confidence=0.99,
            title=_string_or_none(best.get("title")),
            authors=[str(x) for x in best.get("author_name", [])] or None,
            isbn=isbn,
            publisher=_first_text(best.get("publisher")),
            original_publication_date=original_date,
            edition_publication_date=_first_text(best.get("publish_date")),
            language=_first_text(best.get("language")),
            raw={"search_response": response.data, "selected": best},
        )

    def _search(self, *, title: str, author: str) -> MetadataCandidate | None:
        params = urlencode(
            {
                "title": title,
                "author": author,
                "limit": 5,
                "fields": (
                    "key,title,author_name,first_publish_year,isbn,publisher,language,"
                    "first_sentence,edition_key"
                ),
            }
        )
        response = self._get_json(f"https://openlibrary.org/search.json?{params}")
        if response is None:
            return None

        docs = response.data.get("docs") or []
        if not docs:
            return None

        ranked = sorted(
            docs,
            key=lambda doc: _match_score(doc, title=title, author=author),
            reverse=True,
        )
        best = ranked[0]
        score = _match_score(best, title=title, author=author)
        if score < 0.65:
            return None

        first_year = best.get("first_publish_year")
        original_date = str(first_year) if isinstance(first_year, int) else None
        isbns = best.get("isbn") or []

        return MetadataCandidate(
            provider=self.name,
            query_key=f"title-author:{normalize_text(title)}|{normalize_text(author)}",
            source_url=response.url,
            identity_confidence=min(0.95, score),
            title=_string_or_none(best.get("title")),
            authors=[str(x) for x in best.get("author_name", [])] or None,
            isbn=_first_text(isbns),
            publisher=_first_text(best.get("publisher")),
            original_publication_date=original_date,
            language=_first_text(best.get("language")),
            raw={"search_response": response.data, "selected": best},
        )

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        minimum_interval = 1.0 / self.requests_per_second
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)

    def _retry_delay(self, attempt: int, exc: HTTPError | None = None) -> float:
        if exc is not None and exc.headers is not None:
            retry_after = exc.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass
        return min(8.0, 1.0 * (2**attempt))

    def _get_json(self, url: str) -> JsonResponse | None:
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response)
                    return JsonResponse(url=response.geturl(), data=data)
            except HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, exc))
                        continue
                raise ProviderError(f"Open Library returned HTTP {exc.code} for {url}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self._retry_delay(attempt))
                    continue
                raise ProviderError(f"Open Library request failed for {url}: {exc}") from exc

        raise AssertionError("unreachable")


def make_provider(name: str) -> ResearchProvider:
    if name == "stub":
        return StubResearchProvider()
    raise ValueError(f"unknown research provider: {name}")


def make_metadata_provider(
    name: str,
    *,
    openlibrary_contact: str | None = None,
    openlibrary_timeout: float = 15.0,
    openlibrary_max_retries: int = 2,
) -> MetadataProvider:
    if name == "openlibrary":
        return OpenLibraryProvider(
            contact=openlibrary_contact,
            timeout=openlibrary_timeout,
            max_retries=openlibrary_max_retries,
        )
    raise ValueError(f"unknown metadata provider: {name}")


def metadata_query_key(*, title: str, author: str, isbn: str | None) -> str:
    clean_isbn = _clean_isbn(isbn)
    identity = f"title-author:{normalize_text(title)}|{normalize_text(author)}"
    if clean_isbn:
        return f"isbn:{clean_isbn}|{identity}"
    return identity


def _clean_isbn(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isdigit() or ch in "Xx")
    return cleaned.upper() or None


def _first_text(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            text = _string_or_none(item)
            if text:
                return text
        return None
    return _string_or_none(value)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_language(value: Any) -> str | None:
    if not isinstance(value, list):
        return _first_text(value)
    for item in value:
        if isinstance(item, dict) and item.get("key"):
            return str(item["key"]).rsplit("/", 1)[-1]
        text = _string_or_none(item)
        if text:
            return text
    return None


def _match_score(doc: dict[str, Any], *, title: str, author: str) -> float:
    wanted_title = normalize_text(title)
    got_title = normalize_text(_string_or_none(doc.get("title")))
    title_score = 1.0 if wanted_title and wanted_title == got_title else 0.0
    if not title_score and wanted_title and got_title:
        title_score = 0.8 if wanted_title in got_title or got_title in wanted_title else 0.0

    wanted_author = normalize_text(author)
    author_names = [normalize_text(str(x)) for x in doc.get("author_name", [])]
    author_score = 1.0 if wanted_author and wanted_author in author_names else 0.0
    if not author_score and wanted_author:
        if any(wanted_author in name or name in wanted_author for name in author_names if name):
            author_score = 0.8

    return 0.7 * title_score + 0.3 * author_score
