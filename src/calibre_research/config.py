from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ResearchConfig(BaseModel):
    provider: str = "stub"
    max_cost_per_run_usd: float = 5.0
    max_cost_per_book_usd: float = 0.10
    web_search_enabled: bool = True
    reuse_cached_evidence: bool = True
    wikidata: WikidataConfig = Field(default_factory=lambda: WikidataConfig())


class WikidataConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://www.wikidata.org/w/api.php"
    contact: str = "https://github.com/scole66/calibre-research"
    timeout_seconds: float = 15.0
    max_candidates: int = 5
    requests_per_second: float = 1.0
    max_retries: int = 3
    retry_wait_max_seconds: float = 60.0
    maxlag_seconds: int = 5


class OpenLibraryConfig(BaseModel):
    base_url: str
    contact: str | None = None
    max_books_per_run: int
    timeout_seconds: float
    max_retries: int
    retry_wait_multiplier_seconds: float = 1.0
    retry_wait_max_seconds: float = 30.0
    anonymous_requests_per_second: float
    identified_requests_per_second: float


class GoogleBooksConfig(BaseModel):
    base_url: str
    api_key: str | None = None
    api_key_command: list[str] | None = None
    api_key_command_timeout_seconds: float | None = None
    timeout_seconds: float
    max_retries: int
    retry_wait_multiplier_seconds: float = 1.0
    retry_wait_max_seconds: float = 30.0
    requests_per_second: float


class MetadataConfig(BaseModel):
    providers: list[str] = Field(default_factory=list)
    reuse_cached_lookups: bool = True
    openlibrary: OpenLibraryConfig | None = None
    googlebooks: GoogleBooksConfig | None = None


class CalibreConfig(BaseModel):
    library: str | None = None
    executable: str | None = None
    auto_apply_confidence: float = 0.98
    review_confidence: float = 0.80


class AppConfig(BaseModel):
    database: str = "~/.local/share/calibre-research/research.sqlite3"
    rubric: str = "./config/significance-rubric-2.0.yaml"
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    calibre: CalibreConfig = Field(default_factory=CalibreConfig)

    @property
    def database_path(self) -> Path:
        return Path(os.path.expanduser(self.database)).resolve()


def load_config(config_path: str | Path | None = None) -> AppConfig:
    if config_path is not None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file does not exist: {path}")
        data = yaml.safe_load(path.read_text()) or {}
    else:
        default_path = Path.cwd() / "config.yaml"
        data = yaml.safe_load(default_path.read_text()) or {} if default_path.exists() else {}

    return AppConfig.model_validate(data)
