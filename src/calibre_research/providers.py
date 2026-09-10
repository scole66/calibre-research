from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .metadata import MetadataCandidate, normalize_text
from .models import ResearchResult

USER_AGENT = "calibre-research/0.1 (+https://github.com/scole66/calibre-research)"


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

    def __init__(self, *, timeout: float = 15.0):
        self.timeout = timeout

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
        response = self._get_json(f"https://openlibrary.org/isbn/{quote(isbn)}.json")
        if response is None:
            return None

        data = response.data
        authors = self._author_names_from_refs(data.get("authors", []))
        work = self._first_work(data.get("works", []))
        original_date = None
        if work:
            original_date = _string_or_none(work.get("first_publish_date"))

        return MetadataCandidate(
            provider=self.name,
            query_key=f"isbn:{isbn}",
            source_url=response.url,
            identity_confidence=0.99,
            title=_string_or_none(data.get("title")),
            authors=authors or None,
            isbn=isbn,
            publisher=_first_text(data.get("publishers")),
            original_publication_date=original_date,
            edition_publication_date=_string_or_none(data.get("publish_date")),
            language=_first_language(data.get("languages")),
            raw=data,
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

    def _author_names_from_refs(self, refs: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for ref in refs:
            key = ref.get("key")
            if not key:
                continue
            response = self._get_json(f"https://openlibrary.org{key}.json")
            if response and response.data.get("name"):
                names.append(str(response.data["name"]))
        return names

    def _first_work(self, refs: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not refs:
            return None
        key = refs[0].get("key")
        if not key:
            return None
        response = self._get_json(f"https://openlibrary.org{key}.json")
        return response.data if response else None

    def _get_json(self, url: str) -> JsonResponse | None:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
                return JsonResponse(url=response.geturl(), data=data)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise ProviderError(f"Open Library returned HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Open Library request failed for {url}: {exc}") from exc


def make_provider(name: str) -> ResearchProvider:
    if name == "stub":
        return StubResearchProvider()
    raise ValueError(f"unknown research provider: {name}")


def make_metadata_provider(name: str) -> MetadataProvider:
    if name == "openlibrary":
        return OpenLibraryProvider()
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
