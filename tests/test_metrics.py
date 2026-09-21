"""Unit tests for the deterministic analysis layer (no network/Spark needed)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402
from analysis import metrics  # noqa: E402


def _match(**kw):
    base = dict(
        win=True, cs_per_min=6.8, kda=2.5, vision_per_min=0.9,
        gold_per_min=375, kill_participation=0.55,
    )
    base.update(kw)
    return base


def test_aggregate_player_averages_and_winrate():
    agg = metrics.aggregate_player([_match(win=True), _match(win=False, cs_per_min=4.8)])
    assert agg["games"] == 2
    assert agg["winrate"] == 0.5
    assert agg["cs_per_min"] == 5.8  # (6.8 + 4.8) / 2


def test_evaluate_flags_weakness_below_threshold():
    # CS/min well below the MIDDLE benchmark (6.8) should be flagged.
    matches = [_match(cs_per_min=4.0) for _ in range(5)]
    result = metrics.evaluate(matches, role="MIDDLE",
                              benchmarks=metrics.DEFAULT_GOLD_BENCHMARKS)
    weak_metrics = {c.metric for c in result["weaknesses"]}
    assert "cs_per_min" in weak_metrics
    assert result["has_benchmark"] is True


def test_evaluate_no_silent_gold_fallback():
    # No benchmarks for the tier/role -> no comparison, NOT a Gold substitution.
    matches = [_match(cs_per_min=4.0) for _ in range(5)]
    result = metrics.evaluate(matches, role="MIDDLE", benchmarks=None)
    assert result["has_benchmark"] is False
    assert result["comparisons"] == []
    assert result["player"]  # still returns the player's own stats
    # also when the tier dict exists but lacks this role
    result2 = metrics.evaluate(matches, role="TOP", benchmarks={"MIDDLE": {"cs_per_min": 6.8}})
    assert result2["has_benchmark"] is False


def test_evaluate_empty_is_safe():
    result = metrics.evaluate([], role="MIDDLE")
    assert result["player"] == {}
    assert result["comparisons"] == []


def test_filter_by_opponent_tier():
    matches = [
        _match(opponent_tier="GOLD"),
        _match(opponent_tier="SILVER"),
        _match(opponent_tier="GOLD"),
        _match(),  # no opponent_tier key
    ]
    gold = metrics.filter_by_opponent_tier(matches, "GOLD")
    assert len(gold) == 2


def test_benchmarks_from_rows_picks_tier_and_role():
    rows = [
        {"tier": "GOLD", "team_position": "MIDDLE", "cs_per_min": 6.5, "kda": 2.4,
         "vision_per_min": 0.9, "gold_per_min": 370, "kill_participation": 0.54,
         "sample_size": 40},
        {"tier": "SILVER", "team_position": "MIDDLE", "cs_per_min": 5.0, "kda": 2.0,
         "vision_per_min": 0.8, "gold_per_min": 340, "kill_participation": 0.50,
         "sample_size": 30},
    ]
    bench = metrics.benchmarks_from_rows(rows, "GOLD")
    assert set(bench) == {"MIDDLE"}
    assert bench["MIDDLE"]["cs_per_min"] == 6.5
    assert "sample_size" not in bench["MIDDLE"]  # only labeled metrics kept


def test_benchmarks_from_rows_none_when_tier_absent():
    rows = [{"tier": "PLATINUM", "team_position": "TOP", "cs_per_min": 7.0}]
    assert metrics.benchmarks_from_rows(rows, "GOLD") is None


def test_queue_category_classification():
    assert config.queue_category(420) == "ranked"   # solo/duo
    assert config.queue_category(440) == "ranked"   # flex
    assert config.queue_category(400) == "unranked"  # draft normal
    assert config.queue_category(450) == "other"    # ARAM
    assert config.queue_category(None) == "other"


def test_queue_name_friendly_labels():
    assert config.queue_name(420) == "Ranked Solo/Duo"
    assert config.queue_name(400) == "Normal Draft"
    assert config.queue_name(1750) == "Arena"
    assert config.queue_name(450) == "ARAM"
    assert config.queue_name(99999) == "Other (99999)"  # unknown id
    assert config.queue_name(None) == "Unknown"


def test_readiness_summary_practice_balance():
    # Tenet: unranked = practice. 1 ranked + 4 unranked -> 80% practice -> enough.
    matches = (
        [_match(queue_category="ranked")]
        + [_match(queue_category="unranked") for _ in range(4)]
    )
    r = metrics.readiness_summary(matches, "GOLD")
    assert r["ranked_games"] == 1
    assert r["unranked_games"] == 4
    assert r["practice_ratio"] == 0.8
    assert r["practicing_enough"] is True
    assert r["target_tier"] == "GOLD"


def test_challenge_catalog_has_20_metrics():
    assert len(config.CHALLENGE_METRICS) == 20
    assert config.CHALLENGE_KEYS == [m["key"] for m in config.CHALLENGE_METRICS]
    # deaths is the one "lower is better" metric.
    deaths = next(m for m in config.CHALLENGE_METRICS if m["key"] == "deathsByEnemyChamps")
    assert deaths["better"] == "low"


def test_detailed_comparison_verdicts():
    matches = [
        {"laneMinionsFirst10Minutes": 40.0, "deathsByEnemyChamps": 9.0, "kda": 3.5}
    ]
    bench = {"MIDDLE": {
        "laneMinionsFirst10Minutes": 70.0,  # player well below -> "below"
        "deathsByEnemyChamps": 5.0,          # player has MORE deaths -> worse -> "below"
        "kda": 2.0,                          # player above -> "above"
    }}
    rows = {r["key"]: r for r in metrics.detailed_comparison(matches, "MIDDLE", bench)}
    assert len(rows) == 20
    assert rows["laneMinionsFirst10Minutes"]["verdict"] == "below"
    assert rows["deathsByEnemyChamps"]["verdict"] == "below"  # low-better inverted
    assert rows["kda"]["verdict"] == "above"
    # A metric with no benchmark/value -> no_data
    assert rows["soloKills"]["verdict"] == "no_data"


def test_readiness_summary_flags_under_practice():
    # 9 ranked + 1 unranked -> 10% practice -> not enough.
    matches = (
        [_match(queue_category="ranked") for _ in range(9)]
        + [_match(queue_category="unranked")]
    )
    r = metrics.readiness_summary(matches, "GOLD")
    assert r["practicing_enough"] is False
