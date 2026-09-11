from __future__ import annotations

import json
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from .metadata import MetadataCandidate, metadata_query_key, normalize_isbn, normalize_text
from .models import ResearchResult

USER_AGENT_BASE = "calibre-research/0.1 (+https://github.com/scole66/calibre-research)"


class ProviderError(RuntimeError):
    pass


class RetryableProviderError(ProviderError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    """The provider cannot service more requests during this invocation."""


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


class HttpJsonProvider(MetadataProvider):
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        max_retries: int,
        requests_per_second: float,
        retry_wait_multiplier_seconds: float = 1.0,
        retry_wait_max_seconds: float = 30.0,
        user_agent: str = USER_AGENT_BASE,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_wait_multiplier_seconds = retry_wait_multiplier_seconds
        self.retry_wait_max_seconds = retry_wait_max_seconds
        self.requests_per_second = requests_per_second
        self.user_agent = user_agent
        self._last_request_at: float | None = None

    def _wait_for_rate_limit(self) -> None:
        if self.requests_per_second <= 0 or self._last_request_at is None:
            return
        minimum_interval = 1.0 / self.requests_per_second
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < minimum_interval:
            time.sleep(minimum_interval - elapsed)

    def _safe_url(self, url: str) -> str:
        return url

    def _get_json(self, url: str) -> JsonResponse | None:
        retrying = Retrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_random_exponential(
                multiplier=self.retry_wait_multiplier_seconds,
                max=self.retry_wait_max_seconds,
            ),
            retry=retry_if_exception_type(RetryableProviderError),
            reraise=True,
        )
        return retrying(self._get_json_once, url)

    def _get_json_once(self, url: str) -> JsonResponse | None:
        request = Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        self._wait_for_rate_limit()
        try:
            self._last_request_at = time.monotonic()
            with urlopen(request, timeout=self.timeout) as response:
                return JsonResponse(url=response.geturl(), data=json.load(response))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = None
            error = self._http_error(exc.code, url, body)
            raise error from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RetryableProviderError(
                f"{self.name} request failed for {self._safe_url(url)}: {exc}"
            ) from exc

    def _http_error(self, status: int, url: str, body: str | None) -> ProviderError:
        message = f"{self.name} returned HTTP {status} for {self._safe_url(url)}" + (
            f": {body}" if body else ""
        )
        if status == 429 or 500 <= status < 600:
            return RetryableProviderError(message)
        return ProviderError(message)


class OpenLibraryProvider(HttpJsonProvider):
    name = "openlibrary"

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        contact: str | None,
        max_retries: int,
        anonymous_requests_per_second: float,
        identified_requests_per_second: float,
        retry_wait_multiplier_seconds: float = 1.0,
        retry_wait_max_seconds: float = 30.0,
    ):
        user_agent = USER_AGENT_BASE
        requests_per_second = anonymous_requests_per_second
        if contact:
            user_agent = (
                f"calibre-research/0.1 ({contact}; +https://github.com/scole66/calibre-research)"
            )
            requests_per_second = identified_requests_per_second
        super().__init__(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_wait_multiplier_seconds=retry_wait_multiplier_seconds,
            retry_wait_max_seconds=retry_wait_max_seconds,
            requests_per_second=requests_per_second,
            user_agent=user_agent,
        )

    def lookup(
        self,
        *,
        title: str,
        author: str,
        isbn: str | None = None,
    ) -> MetadataCandidate | None:
        query_key = metadata_query_key(title=title, author=author, isbn=isbn)
        clean_isbn = normalize_isbn(isbn)
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
        response = self._get_json(f"{self.base_url}/search.json?{params}")
        if response is None:
            return None
        docs = response.data.get("docs") or []
        if not docs:
            return None
        best = docs[0]
        first_year = best.get("first_publish_year")
        return MetadataCandidate(
            provider=self.name,
            query_key=f"isbn:{isbn}",
            source_url=response.url,
            identity_confidence=0.99,
            title=_string_or_none(best.get("title")),
            authors=[str(x) for x in best.get("author_name", [])] or None,
            isbn=isbn,
            publisher=_first_text(best.get("publisher")),
            original_publication_date=str(first_year) if isinstance(first_year, int) else None,
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
        response = self._get_json(f"{self.base_url}/search.json?{params}")
        if response is None:
            return None
        docs = response.data.get("docs") or []
        if not docs:
            return None
        best = max(docs, key=lambda doc: _match_score(doc, title=title, author=author))
        score = _match_score(best, title=title, author=author)
        if score < 0.65:
            return None
        first_year = best.get("first_publish_year")
        return MetadataCandidate(
            provider=self.name,
            query_key=f"title-author:{normalize_text(title)}|{normalize_text(author)}",
            source_url=response.url,
            identity_confidence=min(0.95, score),
            title=_string_or_none(best.get("title")),
            authors=[str(x) for x in best.get("author_name", [])] or None,
            isbn=_first_text(best.get("isbn") or []),
            publisher=_first_text(best.get("publisher")),
            original_publication_date=str(first_year) if isinstance(first_year, int) else None,
            language=_first_text(best.get("language")),
            raw={"search_response": response.data, "selected": best},
        )


class GoogleBooksProvider(HttpJsonProvider):
    name = "googlebooks"

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        api_key: str | None,
        max_retries: int,
        requests_per_second: float,
        retry_wait_multiplier_seconds: float = 1.0,
        retry_wait_max_seconds: float = 30.0,
    ):
        super().__init__(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_wait_multiplier_seconds=retry_wait_multiplier_seconds,
            retry_wait_max_seconds=retry_wait_max_seconds,
            requests_per_second=requests_per_second,
        )
        self.api_key = api_key
        self._unavailable_error: ProviderUnavailableError | None = None

    def _safe_url(self, url: str) -> str:
        parts = urlsplit(url)
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "key"])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    def lookup(
        self,
        *,
        title: str,
        author: str,
        isbn: str | None = None,
    ) -> MetadataCandidate | None:
        if self._unavailable_error is not None:
            raise self._unavailable_error
        query_key = metadata_query_key(title=title, author=author, isbn=isbn)
        clean_isbn = normalize_isbn(isbn)
        if clean_isbn:
            candidate = self._search(query=f"isbn:{clean_isbn}", title=title, author=author)
            if candidate is not None:
                return replace(candidate, query_key=query_key)
        candidate = self._search(
            query=f'intitle:"{title}" inauthor:"{author}"',
            title=title,
            author=author,
        )
        if candidate is not None:
            return replace(candidate, query_key=query_key)
        return None

    def _http_error(self, status: int, url: str, body: str | None) -> ProviderError:
        if status == 429 and _is_terminal_google_quota_error(body):
            error = ProviderUnavailableError(
                "googlebooks daily quota is disabled or exhausted; "
                "the provider is disabled for the remainder of this run"
            )
            self._unavailable_error = error
            return error
        safe_body = body
        if safe_body and self.api_key:
            safe_body = safe_body.replace(self.api_key, "[REDACTED]")
        return super()._http_error(status, url, safe_body)

    def _search(self, *, query: str, title: str, author: str) -> MetadataCandidate | None:
        params: dict[str, Any] = {"q": query, "maxResults": 5, "printType": "books"}
        if self.api_key:
            params["key"] = self.api_key
        response = self._get_json(f"{self.base_url}/volumes?{urlencode(params)}")
        if response is None:
            return None
        items = response.data.get("items") or []
        if not items:
            return None
        ranked = sorted(
            items,
            key=lambda item: _google_match_score(item, title=title, author=author),
            reverse=True,
        )
        best = ranked[0]
        score = _google_match_score(best, title=title, author=author)
        if score < 0.65:
            return None
        info = best.get("volumeInfo") or {}
        identifiers = info.get("industryIdentifiers") or []
        isbn = _google_isbn(identifiers)
        published = _string_or_none(info.get("publishedDate"))
        return MetadataCandidate(
            provider=self.name,
            query_key="",
            source_url=self._safe_url(response.url),
            identity_confidence=min(0.95, score),
            title=_string_or_none(info.get("title")),
            authors=[str(x) for x in info.get("authors", [])] or None,
            isbn=isbn,
            publisher=_string_or_none(info.get("publisher")),
            original_publication_date=None,
            edition_publication_date=published,
            language=_string_or_none(info.get("language")),
            raw={"search_response": response.data, "selected": best},
        )


def make_provider(name: str) -> ResearchProvider:
    if name == "stub":
        return StubResearchProvider()
    raise ValueError(f"unknown research provider: {name}")


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


def _google_match_score(item: dict[str, Any], *, title: str, author: str) -> float:
    info = item.get("volumeInfo") or {}
    doc = {"title": info.get("title"), "author_name": info.get("authors") or []}
    return _match_score(doc, title=title, author=author)


def _google_isbn(identifiers: list[dict[str, Any]]) -> str | None:
    by_type = {
        str(item.get("type")): _string_or_none(item.get("identifier"))
        for item in identifiers
        if item.get("identifier")
    }
    return by_type.get("ISBN_13") or by_type.get("ISBN_10")


def _is_terminal_google_quota_error(body: str | None) -> bool:
    if not body:
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None

    def values(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key), item
                yield from values(item)
        elif isinstance(value, list):
            for item in value:
                yield from values(item)

    if payload is not None:
        for key, value in values(payload):
            normalized_key = "".join(character for character in key.lower() if character.isalnum())
            if normalized_key == "quotalimitvalue" and str(value).strip() == "0":
                return True
            if normalized_key == "reason" and str(value).lower() == "dailylimitexceeded":
                return True

    lowered = body.lower()
    return "queries per day" in lowered or "daily quota" in lowered


def resolve_api_key(config: dict[str, Any]) -> str:
    command = config.get("api_key_command")
    if command is not None:
        if (
            not command
            or not isinstance(command, list)
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ProviderConfigurationError(
                "googlebooks api_key_command must be a non-empty list of strings"
            )
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=config.get("api_key_command_timeout_seconds"),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderConfigurationError(
                f"googlebooks api_key_command could not be executed: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip()
            suffix = f": {detail}" if detail else ""
            raise ProviderConfigurationError(
                f"googlebooks api_key_command exited with status {result.returncode}{suffix}"
            )
        api_key = result.stdout.strip()
        if not api_key:
            raise ProviderConfigurationError("googlebooks api_key_command returned an empty value")
        return api_key

    api_key = config.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    raise ProviderConfigurationError(
        "googlebooks is enabled but no credentials are configured; "
        "set metadata.googlebooks.api_key_command (preferred) or api_key"
    )


def make_metadata_provider(name: str, config: dict[str, Any]) -> MetadataProvider:
    if name == "openlibrary":
        return OpenLibraryProvider(
            base_url=str(config["base_url"]),
            contact=config.get("contact"),
            timeout=float(config["timeout_seconds"]),
            max_retries=int(config["max_retries"]),
            retry_wait_multiplier_seconds=float(config["retry_wait_multiplier_seconds"]),
            retry_wait_max_seconds=float(config["retry_wait_max_seconds"]),
            anonymous_requests_per_second=float(config["anonymous_requests_per_second"]),
            identified_requests_per_second=float(config["identified_requests_per_second"]),
        )
    if name == "googlebooks":
        return GoogleBooksProvider(
            base_url=str(config["base_url"]),
            api_key=resolve_api_key(config),
            timeout=float(config["timeout_seconds"]),
            max_retries=int(config["max_retries"]),
            retry_wait_multiplier_seconds=float(config["retry_wait_multiplier_seconds"]),
            retry_wait_max_seconds=float(config["retry_wait_max_seconds"]),
            requests_per_second=float(config["requests_per_second"]),
        )
    raise ValueError(f"unknown metadata provider: {name}")
