"""Tests for the deterministic item-build extraction (no network)."""

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from analysis import coach, itemization  # noqa: E402


def _match():
    """A 2-player synthetic match: player (puuid P1, MID, team 100) vs lane
    opponent (puuid P2, MID, team 200)."""
    return {
        "metadata": {"matchId": "NA1_TEST"},
        "info": {
            "queueId": 420,
            "participants": [
                {"puuid": "P1", "participantId": 1, "championName": "Ahri",
                 "teamPosition": "MIDDLE", "teamId": 100, "win": True,
                 "item0": 3157, "item1": 3020, "item2": 0, "item3": 4645,
                 "item4": 0, "item5": 0, "item6": 3340},
                {"puuid": "P2", "participantId": 6, "championName": "Zed",
                 "teamPosition": "MIDDLE", "teamId": 200, "win": False,
                 "item0": 6691, "item1": 3047, "item2": 0, "item3": 0,
                 "item4": 0, "item5": 0, "item6": 3340},
                {"puuid": "P3", "participantId": 2, "championName": "Lee Sin",
                 "teamPosition": "JUNGLE", "teamId": 100, "win": True,
                 "item0": 0, "item1": 0, "item2": 0, "item3": 0,
                 "item4": 0, "item5": 0, "item6": 0},
            ],
        },
    }


def _timeline():
    """Player (participantId 1) buys a start item, undoes a misclick, then later
    buys a core item after the starting window."""
    return {"info": {"frames": [
        {"events": [
            {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 1056, "timestamp": 2000},
            {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 2003, "timestamp": 3000},
            {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 9999, "timestamp": 4000},
            {"type": "ITEM_UNDO", "participantId": 1, "beforeId": 9999, "afterId": 0,
             "timestamp": 4500},
            {"type": "ITEM_PURCHASED", "participantId": 6, "itemId": 1055, "timestamp": 2500},
        ]},
        {"events": [
            {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 3020, "timestamp": 600000},
            {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 2003, "timestamp": 610000},
            {"type": "ITEM_SOLD", "participantId": 1, "itemId": 2003, "timestamp": 620000},
        ]},
    ]}}


def test_final_items_drops_empty_slots():
    facts = itemization.build_facts(_match(), None, "P1")
    assert facts["found"] is True
    # only non-zero slots, in order (incl. trinket 3340)
    assert facts["player"]["final_items"] == [3157, 3020, 4645, 3340]
    assert facts["player"]["champion"] == "Ahri"


def test_lane_opponent_is_same_role_other_team():
    facts = itemization.build_facts(_match(), None, "P1")
    assert facts["opponent"]["champion"] == "Zed"
    assert facts["opponent"]["final_items"] == [6691, 3047, 3340]


def test_no_lane_opponent_when_position_blank():
    match = _match()
    for p in match["info"]["participants"]:
        p["teamPosition"] = ""
    facts = itemization.build_facts(match, None, "P1")
    assert facts["opponent"] is None


def test_starting_items_respect_window_and_undo():
    facts = itemization.build_facts(_match(), _timeline(), "P1")
    # 1056 + 2003 are in-window; 9999 was undone; 3020 is after the window.
    assert facts["starting_items"] == [1056, 2003]


def test_purchase_order_applies_undo_and_sold():
    facts = itemization.build_facts(_match(), _timeline(), "P1")
    # undone 9999 is gone; the SOLD removes the most-recent 2003 (the 610000ms
    # one), leaving the opening 2003 and the 3020 core buy.
    assert facts["purchase_order"] == [1056, 2003, 3020]


def test_build_facts_missing_player():
    assert itemization.build_facts(_match(), None, "NOPE") == {"found": False}


def _fake_classify(iid):
    table = {
        1001: {"boots": True, "real": True},                       # Boots
        6664: {"armor": True, "real": True},                       # an armor item
        3194: {"mr": True, "real": True},                          # a magic-resist item
        3123: {"antiheal": True, "real": True, "component": True}, # Executioner's (anti-heal component)
        1028: {"real": True, "component": True},                   # Ruby Crystal (component)
        3340: {"real": False},                                     # trinket
        2003: {"real": False},                                     # potion
    }
    return table.get(iid, {})


def test_summarize_build():
    s = itemization.summarize_build([1001, 6664, 1028, 3340, 2003], _fake_classify)
    assert s["boots"] is True and s["armor"] is True
    assert s["mr"] is False and s["antiheal"] is False
    assert s["has_component"] is True       # 1028 is an unfinished component
    assert s["real_items"] == 3             # boots + armor + component (trinket/potion excluded)


def test_aggregate_trends():
    g1 = {"champion": "Ahri", "win": True, "starting_named": ["Doran's Ring"],
          "summary": itemization.summarize_build([1001, 6664, 1028], _fake_classify)}
    g2 = {"champion": "Ahri", "win": False, "starting_named": ["Doran's Ring"],
          "summary": itemization.summarize_build([3194, 3123], _fake_classify)}
    t = itemization.aggregate_trends([g1, g2])
    assert t["games"] == 2 and t["wins"] == 1
    assert t["boots_rate"] == 0.5    # only g1 had boots
    assert t["mr_rate"] == 0.5       # only g2 had MR
    assert t["antiheal_rate"] == 0.5
    assert t["component_rate"] == 1.0  # both builds left a component
    assert t["most_common_start"] == ("Doran's Ring", 2)
    assert t["start_variety"] == 1


def test_aggregate_trends_empty():
    assert itemization.aggregate_trends([]) == {"games": 0}


def test_item_trends_template_fallback():
    trends = {"games": 3, "wins": 1, "boots_rate": 0.33, "armor_rate": 1.0,
              "mr_rate": 0.0, "antiheal_rate": 0.0, "component_rate": 0.66,
              "avg_real_items": 4.0, "most_common_start": ("Doran's Ring", 2),
              "start_variety": 2}
    out = coach.analyze_item_trends(trends, [], endpoint="")  # force template
    assert "last 3 games" in out
    assert "33%" in out  # boots rate


def test_itemization_template_fallback_mentions_opponent():
    facts = {
        "player": {"champion": "Ahri", "role": "MIDDLE", "win": True},
        "opponent": {"champion": "Zed"},
        "starting_items_named": ["Doran's Ring", "Health Potion"],
        "final_items_named": ["Luden's Companion", "Sorcerer's Shoes"],
        "opponent_items_named": ["Eclipse"],
    }
    out = coach.analyze_itemization(facts, endpoint="")  # force template
    assert "Zed" in out
    assert "Luden's Companion" in out
