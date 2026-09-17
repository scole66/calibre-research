from __future__ import annotations

from pathlib import Path

import yaml

from .models import AwardEvidence


def load_rubric(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def score_significance(
    *,
    rubric: dict,
    awards: list[AwardEvidence],
) -> tuple[float, dict[str, dict[str, float | str]]]:
    """Score structured evidence deterministically under a versioned rubric."""
    components: dict[str, dict[str, float | str]] = {}

    awards_cfg = rubric["components"]["major_awards"]
    award_points = 0.0
    for award in awards:
        result = award.result.lower()
        if result == "winner":
            award_points += awards_cfg["rules"]["win"]
        elif result in {"nominee", "nominated", "finalist"}:
            award_points += awards_cfg["rules"]["nomination"]
    components["major_awards"] = {
        "score": min(award_points, awards_cfg["max"]),
        "maximum": float(awards_cfg["max"]),
        "status": "assessed" if awards else "unknown",
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
