from calibre_research.models import ResearchResult


def test_research_result_minimal():
    result = ResearchResult(title="Dreamsnake", author="Vonda N. McIntyre", identity_confidence=1.0)
    assert result.claims == []
