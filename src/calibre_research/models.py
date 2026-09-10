from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class EvidenceItem(BaseModel):
    source_type: str
    source_name: str | None = None
    source_url: HttpUrl | None = None
    citation_text: str | None = None


class ResearchedFact(BaseModel):
    field_name: str
    value: object
    confidence: float = Field(ge=0, le=1)
    note: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)


class SignificanceClaim(BaseModel):
    category: str
    claim: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class ResearchResult(BaseModel):
    title: str
    author: str
    identity_confidence: float = Field(ge=0, le=1)
    facts: list[ResearchedFact] = Field(default_factory=list)
    claims: list[SignificanceClaim] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
