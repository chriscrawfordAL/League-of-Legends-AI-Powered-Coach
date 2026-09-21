"""Tests for deterministic timeline analysis (no network)."""

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from analysis import timeline  # noqa: E402


def _tl():
    return {"info": {"frames": [
        {"timestamp": 0, "participantFrames": {
            "1": {"totalGold": 500, "xp": 0, "minionsKilled": 0, "jungleMinionsKilled": 0},
            "6": {"totalGold": 500, "xp": 0, "minionsKilled": 0, "jungleMinionsKilled": 0}},
         "events": []},
        {"timestamp": 600000, "participantFrames": {
            "1": {"totalGold": 4000, "xp": 5000, "minionsKilled": 70, "jungleMinionsKilled": 0},
            "6": {"totalGold": 3500, "xp": 4800, "minionsKilled": 60, "jungleMinionsKilled": 0}},
         "events": [{"type": "CHAMPION_KILL", "killerId": 1, "victimId": 6,
                     "timestamp": 540000, "assistingParticipantIds": []}]},
        {"timestamp": 900000, "participantFrames": {
            "1": {"totalGold": 6000, "xp": 8000, "minionsKilled": 110, "jungleMinionsKilled": 0},
            "6": {"totalGold": 6200, "xp": 8200, "minionsKilled": 120, "jungleMinionsKilled": 0}},
         "events": [{"type": "CHAMPION_KILL", "killerId": 6, "victimId": 1,
                     "timestamp": 840000, "assistingParticipantIds": [2]}]},
    ]}}


def test_lane_diff_series():
    s = timeline.lane_diff_series(_tl(), 1, 6)
    assert [r["minute"] for r in s] == [0, 10, 15]
    at10 = s[1]
    assert at10["gold_diff"] == 500 and at10["cs_diff"] == 10 and at10["xp_diff"] == 200
    assert s[2]["gold_diff"] == -200  # fell behind by 15


def test_lane_diff_series_no_opponent():
    s = timeline.lane_diff_series(_tl(), 1, None)
    assert all(r["gold_diff"] is None for r in s)
    assert s[1]["player_gold"] == 4000  # own gold still tracked


def test_key_stats():
    ks = timeline.key_stats(timeline.lane_diff_series(_tl(), 1, 6))
    assert ks["gold_diff_at_10"] == 500
    assert ks["cs_diff_at_10"] == 10
    assert ks["gold_diff_at_15"] == -200


def test_combat_timeline():
    c = timeline.combat_timeline(_tl(), 1)
    assert c["kills"] == [9]    # killerId 1 at 540000ms
    assert c["deaths"] == [14]  # victimId 1 at 840000ms
    assert c["assists"] == []
    # the assisting player (id 2) sees it as an assist
    assert timeline.combat_timeline(_tl(), 2)["assists"] == [14]
