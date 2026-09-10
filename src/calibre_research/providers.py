from __future__ import annotations

from abc import ABC, abstractmethod
from .models import ResearchResult


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


def make_provider(name: str) -> ResearchProvider:
    if name == "stub":
        return StubResearchProvider()
    raise ValueError(f"unknown research provider: {name}")
