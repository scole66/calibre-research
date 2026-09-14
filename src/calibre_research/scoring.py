from __future__ import annotations

from pathlib import Path

import yaml


def load_rubric(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def score_work(
    *,
    rubric: dict,
    claims_by_category: dict[str, list[dict]],
) -> tuple[float, dict[str, dict[str, float | str]]]:
    """Score structured evidence deterministically under a versioned rubric."""
    components: dict[str, dict[str, float | str]] = {}

    awards_cfg = rubric["components"]["major_awards"]
    award_points = 0.0
    for claim in claims_by_category.get("major_awards", []):
        result = str(claim.get("result", "")).lower()
        if result == "winner":
            award_points += awards_cfg["rules"]["win"]
        elif result in {"nominee", "nominated", "finalist"}:
            award_points += awards_cfg["rules"]["nomination"]
    award_claims = claims_by_category.get("major_awards")
    components["major_awards"] = {
        "score": min(award_points, awards_cfg["max"]),
        "maximum": float(awards_cfg["max"]),
        "status": "assessed" if award_claims else "unknown",
    }

    for name, cfg in rubric["components"].items():
        if name in components:
            continue
        components[name] = {
            "score": 0.0,
            "maximum": float(cfg["max"]),
            "status": "unknown",
        }

    total = sum(float(component["score"]) for component in components.values())
    return total, components


def assessed_maximum(components: dict[str, dict[str, float | str]]) -> float:
    return sum(
        float(component["maximum"])
        for component in components.values()
        if isinstance(component, dict) and component.get("status") == "assessed"
    )
