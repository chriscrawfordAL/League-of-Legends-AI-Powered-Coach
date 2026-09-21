"""Deterministic performance metrics and cohort comparison.

The metrics here are computed exactly (no LLM) so the coaching narrative is
grounded in numbers rather than hallucinated. ``analysis.coach`` consumes the
output of :func:`evaluate` to write the natural-language feedback.

Benchmarks: ideally sourced from the GOLD_RANK_BENCHMARKS table built by the
pipeline. Until that aggregation is enabled, we fall back to the static
reference values below (approximate GOLD solo-queue norms). Swap these out by
passing a ``benchmarks`` dict from the gold table into :func:`evaluate`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Approximate GOLD-tier per-role reference benchmarks. Placeholder until the
# pipeline computes live cohort aggregates into GOLD_RANK_BENCHMARKS.
DEFAULT_GOLD_BENCHMARKS: dict[str, dict[str, float]] = {
    #                     cs_per_min  kda   vision_per_min  gold_per_min  kill_participation
    "TOP":     {"cs_per_min": 6.4, "kda": 2.3, "vision_per_min": 0.8, "gold_per_min": 360, "kill_participation": 0.48},
    "JUNGLE":  {"cs_per_min": 5.2, "kda": 2.6, "vision_per_min": 1.2, "gold_per_min": 350, "kill_participation": 0.60},
    "MIDDLE":  {"cs_per_min": 6.8, "kda": 2.5, "vision_per_min": 0.9, "gold_per_min": 375, "kill_participation": 0.55},
    "BOTTOM":  {"cs_per_min": 7.0, "kda": 2.7, "vision_per_min": 0.8, "gold_per_min": 385, "kill_participation": 0.55},
    "UTILITY": {"cs_per_min": 1.2, "kda": 2.6, "vision_per_min": 2.2, "gold_per_min": 250, "kill_participation": 0.62},
}

# Higher = the player exceeds the benchmark. We flag a metric when the player is
# below benchmark by more than this fraction.
WEAKNESS_THRESHOLD = 0.10
STRENGTH_THRESHOLD = 0.10

METRIC_LABELS = {
    "cs_per_min": "CS per minute",
    "kda": "KDA",
    "vision_per_min": "Vision score per minute",
    "gold_per_min": "Gold per minute",
    "kill_participation": "Kill participation",
}


@dataclass
class MetricComparison:
    metric: str
    label: str
    player: float
    benchmark: float
    delta_pct: float  # (player - benchmark) / benchmark

    @property
    def verdict(self) -> str:
        if self.delta_pct <= -WEAKNESS_THRESHOLD:
            return "weakness"
        if self.delta_pct >= STRENGTH_THRESHOLD:
            return "strength"
        return "on_par"


def filter_by_opponent_tier(matches: list[dict], tier: str) -> list[dict]:
    """Keep only matches whose lane opponent was in ``tier`` (e.g. GOLD)."""
    return [m for m in matches if m.get("opponent_tier") == tier]


# Tenet: unranked is practice toward ranked. We flag the player as
# under-practicing if fewer than this fraction of their games are unranked.
MIN_PRACTICE_RATIO = 0.30


def split_by_category(matches: list[dict]) -> dict[str, list[dict]]:
    """Partition matches into ranked / unranked / other by queue category."""
    out: dict[str, list[dict]] = {"ranked": [], "unranked": [], "other": []}
    for m in matches:
        out.setdefault(m.get("queue_category", "other"), out["other"]).append(m)
    return out


def readiness_summary(matches: list[dict], target_tier: str) -> dict:
    """Ranked-readiness view per the tenet: practice (unranked) vs ranked.

    Returns per-category aggregates, counts, the practice ratio, and whether the
    player is practicing enough — so the coach can advise on both *skill* and
    *practice habits*, not just raw stats.
    """
    split = split_by_category(matches)
    total = len(matches)
    practice_games = len(split["unranked"])
    practice_ratio = practice_games / total if total else 0.0
    return {
        "target_tier": target_tier,
        "ranked": aggregate_player(split["ranked"]),
        "unranked": aggregate_player(split["unranked"]),
        "ranked_games": len(split["ranked"]),
        "unranked_games": practice_games,
        "other_games": len(split["other"]),
        "total_games": total,
        "practice_ratio": practice_ratio,
        "practicing_enough": practice_ratio >= MIN_PRACTICE_RATIO,
    }


def benchmarks_from_rows(rows: list[dict], tier: str) -> dict | None:
    """Build the role->metrics benchmark dict from gold_rank_benchmarks rows.

    Returns None if no rows match the tier, so callers fall back to the static
    DEFAULT_GOLD_BENCHMARKS.
    """
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("tier") != tier:
            continue
        role = r.get("team_position")
        if not role:
            continue
        out[role] = {
            m: r[m] for m in METRIC_LABELS if r.get(m) is not None
        }
    return out or None


def _avg(matches: list[dict], key: str) -> float | None:
    vals = [m[key] for m in matches if m.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def detailed_comparison(
    matches: list[dict],
    role: str,
    challenge_benchmarks: dict | None,
) -> list[dict]:
    """Compare the player's per-game average of each challenge metric to GOLD.

    ``challenge_benchmarks`` maps {role: {metric_key: gold_avg}} (built from the
    gold_challenge_benchmarks table). Returns one row per metric in the catalog
    with the player value, gold average, direction, and a below/above/on-par
    verdict — consumed by the Detailed Metrics view and the per-metric LLM.
    """
    import config

    role_bench = (challenge_benchmarks or {}).get(role, {})
    rows: list[dict] = []
    for spec in config.CHALLENGE_METRICS:
        key = spec["key"]
        player = _avg(matches, key)
        gold = role_bench.get(key)
        delta_pct = None
        verdict = "no_data"
        if player is not None and gold not in (None, 0):
            delta_pct = (player - gold) / abs(gold)
            # For "low is better" (deaths), invert so positive delta = good.
            signed = -delta_pct if spec["better"] == "low" else delta_pct
            verdict = "below" if signed <= -0.10 else ("above" if signed >= 0.10 else "on_par")
        rows.append({
            "key": key,
            "label": spec["label"],
            "better": spec["better"],
            "fmt": spec["fmt"],
            "player": player,
            "gold": gold,
            "delta_pct": delta_pct,
            "verdict": verdict,
        })
    return rows


def aggregate_player(matches: list[dict]) -> dict:
    """Average the per-match player metrics. ``matches`` are gold rows as dicts."""
    if not matches:
        return {}
    keys = ["cs_per_min", "kda", "vision_per_min", "gold_per_min", "kill_participation"]
    out: dict[str, float] = {}
    for k in keys:
        vals = [m[k] for m in matches if m.get(k) is not None]
        out[k] = sum(vals) / len(vals) if vals else 0.0
    out["games"] = len(matches)
    out["winrate"] = sum(1 for m in matches if m.get("win")) / len(matches)
    return out


def _role_benchmarks(role: str, benchmarks: dict | None) -> dict[str, float]:
    # No silent fallback: if the selected tier/role has no benchmark data, return
    # {} so the caller can say so honestly rather than comparing against Gold.
    return (benchmarks or {}).get(role) or {}


def evaluate(
    matches: list[dict],
    role: str = "MIDDLE",
    benchmarks: dict | None = None,
) -> dict:
    """Compare the player's averages to the cohort benchmark for their role.

    Returns a structured summary (player aggregates, per-metric comparisons,
    sorted strengths/weaknesses) suitable for both UI display and the LLM prompt.
    """
    player = aggregate_player(matches)
    if not player:
        return {"role": role, "player": {}, "comparisons": [],
                "weaknesses": [], "strengths": [], "has_benchmark": False}

    bench = _role_benchmarks(role, benchmarks)
    if not bench:
        # No benchmark for this tier/role — show the player's stats, no comparison.
        return {"role": role, "player": player, "comparisons": [],
                "weaknesses": [], "strengths": [], "has_benchmark": False}
    comparisons: list[MetricComparison] = []
    for metric, bval in bench.items():
        pval = player.get(metric, 0.0) or 0.0
        delta = (pval - bval) / bval if bval else 0.0
        comparisons.append(
            MetricComparison(metric, METRIC_LABELS.get(metric, metric), pval, bval, delta)
        )

    weaknesses = sorted(
        [c for c in comparisons if c.verdict == "weakness"], key=lambda c: c.delta_pct
    )
    strengths = sorted(
        [c for c in comparisons if c.verdict == "strength"],
        key=lambda c: c.delta_pct,
        reverse=True,
    )
    return {
        "role": role,
        "player": player,
        "comparisons": comparisons,
        "weaknesses": weaknesses,
        "strengths": strengths,
        "has_benchmark": True,
    }
