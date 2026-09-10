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


class MetadataConfig(BaseModel):
    provider: str = "openlibrary"
    reuse_cached_lookups: bool = True


class CalibreConfig(BaseModel):
    library: str | None = None
    executable: str | None = None
    auto_apply_confidence: float = 0.98
    review_confidence: float = 0.80


class AppConfig(BaseModel):
    database: str = "~/.local/share/calibre-research/research.sqlite3"
    rubric: str = "./config/rubric-1.0.yaml"
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
