from __future__ import annotations

from pathlib import Path

import yaml


def load_rubric(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def score_work(
    *, rubric: dict, claims_by_category: dict[str, list[dict]]
) -> tuple[float, dict[str, float]]:
    """First-pass deterministic scorer.

    Only awards have concrete rules in rubric 1.0. Other categories currently
    accept a precomputed claim score if present, otherwise zero/default.
    """
    components: dict[str, float] = {}

    awards_cfg = rubric["components"]["major_awards"]
    award_points = 0.0
    for claim in claims_by_category.get("major_awards", []):
        result = str(claim.get("result", "")).lower()
        if result == "winner":
            award_points += awards_cfg["rules"]["win"]
        elif result in {"nominee", "nominated", "finalist"}:
            award_points += awards_cfg["rules"]["nomination"]
    components["major_awards"] = min(award_points, awards_cfg["max"])

    for name, cfg in rubric["components"].items():
        if name == "major_awards":
            continue
        default = float(cfg.get("default", 0))
        components[name] = min(default, float(cfg["max"]))

    return sum(components.values()), components
