"""Deterministic item-build extraction from match-v5 payloads.

Pure functions (no network) so they're unit-testable: feed a match dict and a
timeline dict, get the player's build, their lane opponent's build, the starting
items, and the full purchase order. Item-id -> name/icon resolution and the LLM
verdict live elsewhere (app.ddragon, analysis.coach) — this layer only reads the
raw Riot structures.
"""

from __future__ import annotations

from typing import Any

# Purchases at/after this timestamp are no longer the opening buy. Players leave
# the fountain within ~30-60s; 90s comfortably captures the first shop visit and
# excludes the first back's purchases.
STARTING_WINDOW_MS = 90_000


def _final_items(participant: dict) -> list[int]:
    """The 6 item slots + trinket (item6), dropping empty (0) slots, in order."""
    out = []
    for i in range(7):
        iid = participant.get(f"item{i}") or 0
        if iid:
            out.append(int(iid))
    return out


def find_participant(match: dict, puuid: str) -> dict | None:
    """The participant block for ``puuid`` (or None)."""
    for p in match.get("info", {}).get("participants", []):
        if p.get("puuid") == puuid:
            return p
    return None


def find_lane_opponent(match: dict, player: dict) -> dict | None:
    """The enemy in the same lane: matching teamPosition, opposing teamId.

    Returns None for modes without lanes (Arena/ARAM leave teamPosition empty).
    """
    pos = player.get("teamPosition") or ""
    if not pos:
        return None
    team = player.get("teamId")
    for p in match.get("info", {}).get("participants", []):
        if p.get("teamPosition") == pos and p.get("teamId") != team:
            return p
    return None


def purchase_sequence(timeline: dict, participant_id: int) -> list[dict]:
    """Ordered [{itemId, ms}] of items the participant *kept*, undo-adjusted.

    Walks ITEM_PURCHASED / ITEM_UNDO / ITEM_SOLD events in frame order. An UNDO
    removes the most recent matching purchase; a SOLD removes one instance of the
    sold item (so consumables/components that were sold don't pollute the build).
    """
    kept: list[dict] = []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("participantId") != participant_id:
                continue
            etype = ev.get("type")
            if etype == "ITEM_PURCHASED":
                kept.append({"itemId": int(ev.get("itemId", 0)),
                             "ms": int(ev.get("timestamp", 0))})
            elif etype == "ITEM_UNDO":
                before = ev.get("beforeId")
                for i in range(len(kept) - 1, -1, -1):
                    if kept[i]["itemId"] == before:
                        kept.pop(i)
                        break
            elif etype == "ITEM_SOLD":
                sold = ev.get("itemId")
                for i in range(len(kept) - 1, -1, -1):
                    if kept[i]["itemId"] == sold:
                        kept.pop(i)
                        break
    return [k for k in kept if k["itemId"]]


def starting_items(timeline: dict, participant_id: int) -> list[int]:
    """Item ids bought in the opening window (before the first back)."""
    seq = purchase_sequence(timeline, participant_id)
    return [p["itemId"] for p in seq if p["ms"] < STARTING_WINDOW_MS]


def build_facts(match: dict, timeline: dict | None, puuid: str) -> dict[str, Any]:
    """Structured, name-free item facts for one player's game.

    Returns {found, player:{champion, role, win, final_items[]},
    opponent:{champion, role, final_items[]} | None, starting_items[],
    purchase_order[]}. ``found`` is False if the puuid isn't in the match.
    """
    player = find_participant(match, puuid)
    if not player:
        return {"found": False}
    opp = find_lane_opponent(match, player)
    pid = player.get("participantId")
    seq = purchase_sequence(timeline, pid) if timeline and pid else []
    return {
        "found": True,
        "player": {
            "champion": player.get("championName"),
            "role": player.get("teamPosition") or "",
            "win": bool(player.get("win")),
            "final_items": _final_items(player),
            "participant_id": pid,
        },
        "opponent": (None if not opp else {
            "champion": opp.get("championName"),
            "role": opp.get("teamPosition") or "",
            "final_items": _final_items(opp),
            "participant_id": opp.get("participantId"),
        }),
        "starting_items": [p["itemId"] for p in seq if p["ms"] < STARTING_WINDOW_MS],
        "purchase_order": [p["itemId"] for p in seq],
    }


# --------------------------------------------------------------------------
# Build trends across multiple games (#2). Item classification is injected as a
# `classify(item_id) -> dict` callable so this layer stays pure/testable; the app
# supplies one backed by Data Dragon item tags.
# --------------------------------------------------------------------------
def summarize_build(final_item_ids: list[int], classify) -> dict[str, bool]:
    """Boolean coverage flags for one game's final build via ``classify``.

    ``classify(item_id)`` returns {boots, armor, mr, antiheal, component, real}
    where ``real`` marks a non-trinket/non-consumable item that counts toward the
    build and ``component`` marks an unfinished item left in the build.
    """
    flags = {"boots": False, "armor": False, "mr": False, "antiheal": False,
             "has_component": False}
    real_count = 0
    for iid in final_item_ids:
        c = classify(iid) or {}
        if c.get("real"):
            real_count += 1
        for k in ("boots", "armor", "mr", "antiheal"):
            if c.get(k):
                flags[k] = True
        if c.get("component"):
            flags["has_component"] = True
    flags["real_items"] = real_count
    return flags


def aggregate_trends(per_game: list[dict]) -> dict[str, Any]:
    """Aggregate per-game build summaries into trend rates.

    Each element of ``per_game`` is {champion, role, win, summary(from
    summarize_build), starting_named:[...]}. Returns counts/rates the coach
    narrates: how often boots/armor/MR/anti-heal were built, average completed
    items, leftover-component rate, and the most common starting set.
    """
    n = len(per_game)
    if not n:
        return {"games": 0}
    from collections import Counter

    def rate(key):
        return sum(1 for g in per_game if g["summary"].get(key)) / n

    starts = Counter(" + ".join(g.get("starting_named") or []) or "—" for g in per_game)
    return {
        "games": n,
        "wins": sum(1 for g in per_game if g.get("win")),
        "boots_rate": rate("boots"),
        "armor_rate": rate("armor"),
        "mr_rate": rate("mr"),
        "antiheal_rate": rate("antiheal"),
        "component_rate": rate("has_component"),
        "avg_real_items": sum(g["summary"].get("real_items", 0) for g in per_game) / n,
        "most_common_start": starts.most_common(1)[0],
        "start_variety": len(starts),
        "champions": [g.get("champion") for g in per_game],
    }
