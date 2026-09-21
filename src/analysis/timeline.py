"""Deterministic match-timeline analysis (no network).

The match-v5 timeline gives per-minute ``participantFrames`` (gold, xp, cs, level)
and ``events`` (kills, etc.). We already fetch it for item purchases; these pure
functions turn it into the lane-diff curves + combat timing the coach narrates.
Feed a timeline dict + the player's and opponent's participantId (1-10).
"""

from __future__ import annotations

from typing import Any


def _pframe(frame: dict, pid: int) -> dict:
    return (frame.get("participantFrames", {}) or {}).get(str(pid), {}) or {}


def _cs(pf: dict) -> int:
    return int(pf.get("minionsKilled", 0) or 0) + int(pf.get("jungleMinionsKilled", 0) or 0)


def lane_diff_series(timeline: dict, player_id: int, opponent_id: int | None) -> list[dict]:
    """Per-frame [{minute, player_gold, gold_diff, xp_diff, cs_diff, player_cs}].

    Diffs are player-minus-opponent; when there's no lane opponent the *_diff
    fields are None but the player's own gold/cs are still returned.
    """
    out = []
    for frame in timeline.get("info", {}).get("frames", []):
        pf = _pframe(frame, player_id)
        if not pf:
            continue
        minute = round(int(frame.get("timestamp", 0)) / 60000)
        row = {"minute": minute, "player_gold": int(pf.get("totalGold", 0) or 0),
               "player_cs": _cs(pf), "gold_diff": None, "xp_diff": None, "cs_diff": None}
        if opponent_id:
            of = _pframe(frame, opponent_id)
            if of:
                row["gold_diff"] = row["player_gold"] - int(of.get("totalGold", 0) or 0)
                row["xp_diff"] = int(pf.get("xp", 0) or 0) - int(of.get("xp", 0) or 0)
                row["cs_diff"] = row["player_cs"] - _cs(of)
        out.append(row)
    return out


def _at_minute(series: list[dict], minute: int) -> dict | None:
    """The frame nearest a target minute (timelines are ~1/min, sometimes sparse)."""
    if not series:
        return None
    return min(series, key=lambda r: abs(r["minute"] - minute))


def key_stats(series: list[dict]) -> dict[str, Any]:
    """Headline gold/CS (and diffs) at the 10- and 15-minute marks."""
    out: dict[str, Any] = {}
    for mark in (10, 15):
        row = _at_minute(series, mark)
        if row is None or row["minute"] < mark - 2:  # game ended before this mark
            continue
        out[f"gold_at_{mark}"] = row["player_gold"]
        out[f"cs_at_{mark}"] = row["player_cs"]
        out[f"gold_diff_at_{mark}"] = row["gold_diff"]
        out[f"cs_diff_at_{mark}"] = row["cs_diff"]
    return out


def combat_timeline(timeline: dict, player_id: int) -> dict[str, list[int]]:
    """Minutes at which the player got a kill, died, or assisted."""
    kills, deaths, assists = [], [], []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") != "CHAMPION_KILL":
                continue
            minute = round(int(ev.get("timestamp", 0)) / 60000)
            if ev.get("killerId") == player_id:
                kills.append(minute)
            elif ev.get("victimId") == player_id:
                deaths.append(minute)
            elif player_id in (ev.get("assistingParticipantIds") or []):
                assists.append(minute)
    return {"kills": kills, "deaths": deaths, "assists": assists}
