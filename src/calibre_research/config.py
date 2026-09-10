from __future__ import annotations

from pathlib import Path
import os
import yaml
from pydantic import BaseModel, Field


class ResearchConfig(BaseModel):
    provider: str = "stub"
    max_cost_per_run_usd: float = 5.0
    max_cost_per_book_usd: float = 0.10
    web_search_enabled: bool = True
    reuse_cached_evidence: bool = True


class CalibreConfig(BaseModel):
    library: str | None = None
    auto_apply_confidence: float = 0.98
    review_confidence: float = 0.80


class AppConfig(BaseModel):
    database: str = "~/.local/share/calibre-research/research.sqlite3"
    rubric: str = "./config/rubric-1.0.yaml"
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    calibre: CalibreConfig = Field(default_factory=CalibreConfig)

    @property
    def database_path(self) -> Path:
        return Path(os.path.expanduser(self.database)).resolve()


def load_config(path: Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    data = yaml.safe_load(path.read_text()) or {}
    return AppConfig.model_validate(data)
