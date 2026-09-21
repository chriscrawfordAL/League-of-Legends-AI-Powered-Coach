"""Flattening helpers that turn raw Riot match JSON into tabular rows.

Kept separate from the HTTP client so the pipeline can transform either freshly
fetched payloads or rows replayed from the bronze table.
"""

from __future__ import annotations

from typing import Any

import config


def participant_rows(match: dict) -> list[dict]:
    """One flat row per participant for a match-v5 payload.

    Only the fields the coach cares about are projected; the full payload is
    still retained in bronze for reprocessing.
    """
    info = match.get("info", {})
    metadata = match.get("metadata", {})
    match_id = metadata.get("matchId")
    game_duration_s = info.get("gameDuration", 0) or 0
    minutes = max(game_duration_s / 60.0, 1e-9)

    rows: list[dict] = []
    for p in info.get("participants", []):
        cs = (p.get("totalMinionsKilled", 0) or 0) + (
            p.get("neutralMinionsKilled", 0) or 0
        )
        challenges: dict[str, Any] = p.get("challenges", {}) or {}
        # Riot may send killParticipation as an int (0) or float (0.5); coerce to
        # float so Spark infers a consistent DoubleType across rows.
        kp = challenges.get("killParticipation")
        kp = float(kp) if kp is not None else None
        # 20 high-signal challenge metrics for the Detailed Metrics view. Coerce
        # all to float (Riot mixes int/float) so Spark infers a stable schema.
        challenge_cols = {}
        for key in config.CHALLENGE_KEYS:
            v = challenges.get(key)
            challenge_cols[key] = float(v) if v is not None else None
        rows.append(
            {
                **challenge_cols,
                "match_id": match_id,
                "queue_id": info.get("queueId"),
                "queue_category": config.queue_category(info.get("queueId")),
                "game_creation": info.get("gameCreation"),
                "game_duration_s": game_duration_s,
                "puuid": p.get("puuid"),
                "team_id": p.get("teamId"),  # 100/200; needed to find lane opponent
                "riot_id_game_name": p.get("riotIdGameName"),
                "riot_id_tagline": p.get("riotIdTagline"),
                "champion": p.get("championName"),
                "team_position": p.get("teamPosition"),
                "win": bool(p.get("win")),
                "kills": p.get("kills", 0),
                "deaths": p.get("deaths", 0),
                "assists": p.get("assists", 0),
                "kda": kda(p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0)),
                "cs": cs,
                "cs_per_min": cs / minutes,
                "gold_earned": p.get("goldEarned", 0),
                "gold_per_min": (p.get("goldEarned", 0) or 0) / minutes,
                "vision_score": p.get("visionScore", 0),
                "vision_per_min": (p.get("visionScore", 0) or 0) / minutes,
                "damage_to_champions": p.get("totalDamageDealtToChampions", 0),
                "kill_participation": kp,
            }
        )
    return rows


def kda(kills: int, deaths: int, assists: int) -> float:
    """KDA ratio with the conventional deaths=0 -> treat as 1 guard."""
    return (kills + assists) / max(deaths, 1)


# Ranked tiers low -> high, for ordering/comparison.
TIER_ORDER = [
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
]


def rank_row(puuid: str, entries: list[dict], queue_type: str = "RANKED_SOLO_5x5") -> dict:
    """Project a league-v4 entries response into a single cached rank row.

    Picks the requested queue (solo by default). Unranked -> tier=None so the
    pipeline can still record that the puuid was looked up (avoids refetching).
    """
    chosen = next((e for e in entries if e.get("queueType") == queue_type), None)
    return {
        "puuid": puuid,
        "queue_type": queue_type,
        "tier": (chosen or {}).get("tier"),
        "division": (chosen or {}).get("rank"),
        "league_points": (chosen or {}).get("leaguePoints"),
    }
