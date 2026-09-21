"""Tests for the app-side per-player table write helpers (pure logic, no warehouse)."""

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "app"))

import data_access  # noqa: E402


def test_sql_lit():
    assert data_access._sql_lit(None) == "NULL"
    assert data_access._sql_lit(True) == "TRUE"
    assert data_access._sql_lit(False) == "FALSE"
    assert data_access._sql_lit(5) == "5"
    assert data_access._sql_lit(1.5) == "1.5"
    assert data_access._sql_lit("Faker") == "'Faker'"
    # single quotes are escaped (SQL injection / names like O'Brien)
    assert data_access._sql_lit("O'Brien") == "'O''Brien'"
    assert data_access._sql_lit(float("nan")) == "NULL"
    assert data_access._sql_lit(float("inf")) == "NULL"


def _cached_rows():
    # game_creation in epoch ms; mix of ranked / unranked / other queues.
    return [
        {"match_id": "A", "game_creation": 2_000_000_000_000, "queue_category": "ranked"},
        {"match_id": "B", "game_creation": 1_000_000_000_000, "queue_category": "unranked"},
        {"match_id": "C", "game_creation": 1_500_000_000_000, "queue_category": "other"},
    ]


def test_filter_cached_timeframe():
    import callbacks  # noqa: E402
    # start_time (s) just under row A's creation -> only A survives.
    start_s = 1_900_000_000
    out = callbacks._filter_cached(_cached_rows(), start_s, "both")
    assert [r["match_id"] for r in out] == ["A"]


def test_filter_cached_queue_mode():
    import callbacks
    rows = _cached_rows()
    assert {r["match_id"] for r in callbacks._filter_cached(rows, None, "ranked")} == {"A"}
    assert {r["match_id"] for r in callbacks._filter_cached(rows, None, "unranked")} == {"B"}
    # "both" excludes the ARAM/bot "other" row C
    assert {r["match_id"] for r in callbacks._filter_cached(rows, None, "both")} == {"A", "B"}


def test_item_href_carries_filters():
    import callbacks
    from urllib.parse import parse_qs
    href = callbacks._item_href("Last Starfighter", "NA2", 7, "both")
    qs = parse_qs(href.lstrip("?"))
    assert qs["view"] == ["itemization"]
    assert qs["s"] == ["Last Starfighter"]   # parse_qs decodes the %20
    assert qs["r"] == ["NA2"] and qs["days"] == ["7"] and qs["q"] == ["both"]
    assert "m" not in qs
    # with a match id selected
    assert "&m=NA2_123" in callbacks._item_href("X", "NA2", 0, "ranked", "NA2_123")


def test_parse_item_filters():
    import callbacks
    from urllib.parse import parse_qs
    assert callbacks._parse_item_filters(parse_qs("days=7&q=ranked")) == (7, "ranked")
    # missing -> safe defaults
    assert callbacks._parse_item_filters(parse_qs("s=X")) == (0, "both")
    # garbage days -> 0
    assert callbacks._parse_item_filters(parse_qs("days=abc")) == (0, "both")


def test_itemization_rows_prefers_player_store():
    import callbacks
    store = {"rows": [{"match_id": "A"}, {"match_id": "B"}], "label": "Foo#NA1"}
    out = callbacks._itemization_rows(store, "Foo", "NA1", 7, "both")
    assert [r["match_id"] for r in out] == ["A", "B"]  # used the live store, no DB


def test_itemization_rows_falls_back_to_table(monkeypatch):
    import callbacks
    # store is for a different player -> must fall back to the persisted table
    monkeypatch.setattr(callbacks.data_access, "load_summoner_table",
                        lambda s, r: [{"match_id": "T", "game_creation": 0,
                                       "queue_category": "ranked"}])
    out = callbacks._itemization_rows({"rows": [{"match_id": "A"}], "label": "Other#NA1"},
                                      "Foo", "NA1", 0, "both")
    assert [r["match_id"] for r in out] == ["T"]


def test_summoner_columns():
    cols, coltype = data_access._summoner_columns()
    # "kda" exists once (base), not duplicated by the challenge key of the same name
    assert cols.count("kda") == 1
    # both the base snake_case and the challenge camelCase coexist as distinct cols
    assert "kill_participation" in cols
    assert "killParticipation" in cols
    assert "cs_per_min" in cols
    # challenge metrics are typed DOUBLE
    assert coltype["laneMinionsFirst10Minutes"] == "DOUBLE"
    assert coltype["win"] == "BOOLEAN"
    assert coltype["match_id"] == "STRING"
