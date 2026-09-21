"""Deterministic team/game-level (macro) analysis from a match + its timeline.

Pure functions (no network, no Data Dragon) so they're unit-testable. They turn
the match-v5 payload + timeline into the team-level facts a macro coach reasons
about: objectives & timings, gold/XP swing, vision, damage profiles (who the AD/AP
threats are), lane outcomes @14, teamfight/death clustering, and rough positional
inferences (support roam, split-push isolation) that are explicitly low-confidence.

Item-name/resistance resolution (needs Data Dragon) is layered on in the app
(`data_access.fetch_macro`); the LLM narrative is in `analysis.coach`.
"""

from __future__ import annotations

import math
from typing import Any

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
BLUE, RED = 100, 200


def _minute(ms) -> int:
    return round(int(ms or 0) / 60000)


def _dist(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    return math.hypot((a.get("x", 0) - b.get("x", 0)), (a.get("y", 0) - b.get("y", 0)))


def participants(match: dict) -> list[dict]:
    """Normalized per-participant rows (the fields macro analysis needs)."""
    out = []
    for p in match.get("info", {}).get("participants", []):
        cs = (p.get("totalMinionsKilled", 0) or 0) + (p.get("neutralMinionsKilled", 0) or 0)
        items = [p.get(f"item{i}") or 0 for i in range(7)]
        out.append({
            "pid": p.get("participantId"), "team": p.get("teamId"),
            "role": p.get("teamPosition") or "", "champion": p.get("championName"),
            "puuid": p.get("puuid"), "win": bool(p.get("win")),
            "kills": p.get("kills", 0), "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0), "gold": p.get("goldEarned", 0), "cs": cs,
            "vision_score": p.get("visionScore", 0), "wards_placed": p.get("wardsPlaced", 0),
            "wards_killed": p.get("wardsKilled", 0),
            "control_wards": p.get("detectorWardsPlaced", 0),
            "phys": p.get("physicalDamageDealtToChampions", 0),
            "magic": p.get("magicDamageDealtToChampions", 0),
            "true": p.get("trueDamageDealtToChampions", 0),
            "total_dmg": p.get("totalDamageDealtToChampions", 0),
            "dmg_taken": p.get("totalDamageTaken", 0),
            "cc_time": p.get("timeCCingOthers", 0), "level": p.get("champLevel", 0),
            "items": [i for i in items if i],
        })
    return out


def team_overview(match: dict) -> dict[int, dict]:
    info = match.get("info", {})
    ps = participants(match)
    out: dict[int, dict] = {}
    for team_id in (BLUE, RED):
        members = [p for p in ps if p["team"] == team_id]
        t = next((t for t in info.get("teams", []) if t.get("teamId") == team_id), {})
        out[team_id] = {
            "team_id": team_id,
            "side": "Blue" if team_id == BLUE else "Red",
            "win": bool(t.get("win")),
            "kills": sum(p["kills"] for p in members),
            "deaths": sum(p["deaths"] for p in members),
            "assists": sum(p["assists"] for p in members),
            "gold": sum(p["gold"] for p in members),
            "vision_score": sum(p["vision_score"] for p in members),
            "champions": {p["role"]: p["champion"] for p in members if p["role"]},
            "members": members,
            "bans": [b.get("championId") for b in t.get("bans", [])],
        }
    return out


def _team_obj(match: dict, team_id: int, key: str) -> dict:
    t = next((t for t in match.get("info", {}).get("teams", []) if t.get("teamId") == team_id), {})
    return (t.get("objectives", {}) or {}).get(key, {}) or {}


def objectives(match: dict, timeline: dict | None) -> dict[str, Any]:
    """Per-team objective counts/first-flags (from match) + timings/soul (timeline)."""
    out: dict[str, Any] = {}
    for team_id in (BLUE, RED):
        out[team_id] = {
            "first_blood": bool(_team_obj(match, team_id, "champion").get("first")),
            "towers": _team_obj(match, team_id, "tower").get("kills", 0),
            "first_tower": bool(_team_obj(match, team_id, "tower").get("first")),
            "inhibitors": _team_obj(match, team_id, "inhibitor").get("kills", 0),
            "dragons": _team_obj(match, team_id, "dragon").get("kills", 0),
            "first_dragon": bool(_team_obj(match, team_id, "dragon").get("first")),
            "heralds": _team_obj(match, team_id, "riftHerald").get("kills", 0),
            "barons": _team_obj(match, team_id, "baron").get("kills", 0),
            "grubs": _team_obj(match, team_id, "horde").get("kills", 0),
            "dragon_types": [],
            "soul": False,
        }
    events: list[dict] = []
    if timeline:
        for frame in timeline.get("info", {}).get("frames", []):
            for ev in frame.get("events", []):
                et = ev.get("type")
                if et == "ELITE_MONSTER_KILL":
                    team = ev.get("killerTeamId")
                    minute = _minute(ev.get("timestamp"))
                    mtype = ev.get("monsterType")
                    sub = ev.get("monsterSubType")
                    events.append({"minute": minute, "team": team, "type": mtype,
                                   "subtype": sub})
                    if team in out and mtype == "DRAGON" and sub:
                        out[team]["dragon_types"].append(
                            sub.replace("_DRAGON", "").replace("_", " ").title())
                elif et == "DRAGON_SOUL_GIVEN":
                    team = ev.get("teamId")
                    if team in out:
                        out[team]["soul"] = True
                    events.append({"minute": _minute(ev.get("timestamp")), "team": team,
                                   "type": "DRAGON_SOUL"})
                elif et == "BUILDING_KILL":
                    owner = ev.get("teamId")  # team that LOST the building
                    taker = RED if owner == BLUE else BLUE
                    events.append({"minute": _minute(ev.get("timestamp")), "team": taker,
                                   "type": ev.get("buildingType"), "lane": ev.get("laneType")})
    out["events"] = sorted(events, key=lambda e: e["minute"])
    return out


def _pframe(frame: dict, pid: int) -> dict:
    return (frame.get("participantFrames", {}) or {}).get(str(pid), {}) or {}


def _frame_at(frames: list[dict], minute: int) -> dict | None:
    if not frames:
        return None
    return min(frames, key=lambda f: abs(_minute(f.get("timestamp")) - minute))


def gold_xp_series(match: dict, timeline: dict | None) -> dict[str, Any]:
    """Blue-minus-Red total gold & XP per minute, plus swing/throw/comeback summary."""
    if not timeline:
        return {"series": [], "summary": {}}
    ps = {p["pid"]: p["team"] for p in participants(match)}
    series = []
    for frame in timeline.get("info", {}).get("frames", []):
        g = {BLUE: 0, RED: 0}
        x = {BLUE: 0, RED: 0}
        pf = frame.get("participantFrames", {}) or {}
        for sid, fr in pf.items():
            team = ps.get(int(sid))
            if team in g:
                g[team] += int(fr.get("totalGold", 0) or 0)
                x[team] += int(fr.get("xp", 0) or 0)
        series.append({"minute": _minute(frame.get("timestamp")),
                       "gold_diff": g[BLUE] - g[RED], "xp_diff": x[BLUE] - x[RED]})
    summary: dict[str, Any] = {}
    if series:
        diffs = [s["gold_diff"] for s in series]
        max_blue = max(diffs)
        max_red = min(diffs)
        final = diffs[-1]
        # The "swing": the minute the gold lead crossed zero last (lead changed hands).
        swing = None
        for i in range(1, len(diffs)):
            if (diffs[i] >= 0) != (diffs[i - 1] >= 0):
                swing = series[i]["minute"]
        winner_blue = team_overview(match)[BLUE]["win"]
        # Threw: had a sizable lead but lost. Comeback: was behind but won.
        threw = (max_blue > 5000 and not winner_blue) or (max_red < -5000 and winner_blue)
        comeback = (max_red < -5000 and not winner_blue) or (max_blue > 5000 and winner_blue) and False
        summary = {"max_blue_lead": max_blue, "max_red_lead": -max_red,
                   "final_gold_diff": final, "swing_minute": swing,
                   "threw_lead": bool(threw)}
    return {"series": series, "summary": summary}


def vision(match: dict) -> dict[int, dict]:
    ps = participants(match)
    out = {}
    for team_id in (BLUE, RED):
        members = [p for p in ps if p["team"] == team_id]
        out[team_id] = {
            "vision_score": sum(p["vision_score"] for p in members),
            "wards_placed": sum(p["wards_placed"] for p in members),
            "wards_killed": sum(p["wards_killed"] for p in members),
            "control_wards": sum(p["control_wards"] for p in members),
        }
    return out


def _dmg_type(phys: int, magic: int, true_: int) -> str:
    total = phys + magic + true_
    if total <= 0:
        return "none"
    if phys >= 0.65 * total:
        return "AD"
    if magic >= 0.65 * total:
        return "AP"
    return "mixed"


def damage_profiles(match: dict) -> dict[int, dict]:
    """Per team: each champ's damage split + the biggest AD and AP threats."""
    ps = participants(match)
    out = {}
    for team_id in (BLUE, RED):
        members = sorted([p for p in ps if p["team"] == team_id],
                         key=lambda p: p["total_dmg"], reverse=True)
        rows = [{"champion": p["champion"], "role": p["role"], "phys": p["phys"],
                 "magic": p["magic"], "true": p["true"], "total": p["total_dmg"],
                 "type": _dmg_type(p["phys"], p["magic"], p["true"])} for p in members]
        ad = max((r for r in rows if r["type"] in ("AD", "mixed")),
                 key=lambda r: r["phys"], default=None)
        ap = max((r for r in rows if r["type"] in ("AP", "mixed")),
                 key=lambda r: r["magic"], default=None)
        out[team_id] = {"rows": rows, "biggest_ad": ad, "biggest_ap": ap}
    return out


def lane_outcomes(match: dict, timeline: dict | None, mark: int = 14) -> list[dict]:
    """Per-role Blue-vs-Red gold/cs/xp differential around `mark` minutes."""
    ps = participants(match)
    frame = _frame_at(timeline.get("info", {}).get("frames", []), mark) if timeline else None
    out = []
    for role in ROLES:
        b = next((p for p in ps if p["team"] == BLUE and p["role"] == role), None)
        r = next((p for p in ps if p["team"] == RED and p["role"] == role), None)
        if not b or not r:
            continue
        row = {"role": role, "blue": b["champion"], "red": r["champion"],
               "gold_diff": None, "cs_diff": None, "xp_diff": None, "winner": None}
        if frame:
            bf, rf = _pframe(frame, b["pid"]), _pframe(frame, r["pid"])
            if bf and rf:
                gd = int(bf.get("totalGold", 0)) - int(rf.get("totalGold", 0))
                row["gold_diff"] = gd
                row["cs_diff"] = (int(bf.get("minionsKilled", 0)) + int(bf.get("jungleMinionsKilled", 0))) - \
                                 (int(rf.get("minionsKilled", 0)) + int(rf.get("jungleMinionsKilled", 0)))
                row["xp_diff"] = int(bf.get("xp", 0)) - int(rf.get("xp", 0))
                row["winner"] = "Blue" if gd > 300 else ("Red" if gd < -300 else "Even")
        out.append(row)
    return out


def teamfights(match: dict, timeline: dict | None, gap_s: int = 20) -> dict[str, Any]:
    """Cluster CHAMPION_KILL events into fights (kills within `gap_s` of each other)."""
    if not timeline:
        return {"fights": [], "summary": {}}
    team_of = {p["pid"]: p["team"] for p in participants(match)}
    kills = []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") == "CHAMPION_KILL":
                kills.append({"ts": int(ev.get("timestamp", 0)),
                              "victim_team": team_of.get(ev.get("victimId"))})
    kills.sort(key=lambda k: k["ts"])
    fights, cur = [], []
    for k in kills:
        if cur and k["ts"] - cur[-1]["ts"] > gap_s * 1000:
            fights.append(cur)
            cur = []
        cur.append(k)
    if cur:
        fights.append(cur)

    rows, blue_won, red_won, aces = [], 0, 0, 0
    for f in fights:
        if len(f) < 3:  # skirmish, not a teamfight
            continue
        # A victim on RED is a kill FOR blue, and vice-versa.
        blue_k = sum(1 for k in f if k["victim_team"] == RED)
        red_k = sum(1 for k in f if k["victim_team"] == BLUE)
        result = "Blue" if blue_k > red_k else ("Red" if red_k > blue_k else "Even")
        if result == "Blue":
            blue_won += 1
        elif result == "Red":
            red_won += 1
        if red_k >= 5 or blue_k >= 5:
            aces += 1
        rows.append({"minute": _minute(f[0]["ts"]), "blue_kills": blue_k,
                     "red_kills": red_k, "result": result})
    return {"fights": rows, "summary": {"total": len(rows), "blue_won": blue_won,
                                        "red_won": red_won, "aces": aces}}


def death_analysis(match: dict, timeline: dict | None, trade_s: int = 10) -> dict[int, dict]:
    """Per team: total deaths and deaths with no ally takedown within `trade_s`."""
    out = {BLUE: {"deaths": 0, "deaths_without_trade": 0},
           RED: {"deaths": 0, "deaths_without_trade": 0}}
    if not timeline:
        return out
    team_of = {p["pid"]: p["team"] for p in participants(match)}
    kills = []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") == "CHAMPION_KILL":
                kills.append({"ts": int(ev.get("timestamp", 0)),
                              "victim_team": team_of.get(ev.get("victimId"))})
    kills.sort(key=lambda k: k["ts"])
    for i, k in enumerate(kills):
        vt = k["victim_team"]
        if vt not in out:
            continue
        out[vt]["deaths"] += 1
        # A "trade" = the victim's team gets a kill (enemy victim) within trade_s.
        traded = any(0 <= kj["ts"] - k["ts"] <= trade_s * 1000 and kj["victim_team"] != vt
                     and kj["victim_team"] is not None for kj in kills[i + 1:i + 8])
        if not traded:
            out[vt]["deaths_without_trade"] += 1
    return out


def positions(match: dict, timeline: dict | None) -> dict[str, Any]:
    """Rough, low-confidence positional reads: support roam + split-push isolation.

    Support roam: fraction of pre-14-min frames the UTILITY is far (>2800u) from
    their own BOTTOM laner. Isolation: fraction of post-15-min frames a player is
    far (>3500u) from their nearest living teammate (a split-push proxy).
    """
    if not timeline:
        return {"support_roam": {}, "isolation": {}}
    ps = participants(match)
    frames = timeline.get("info", {}).get("frames", [])
    by_team_role = {(p["team"], p["role"]): p["pid"] for p in ps if p["role"]}
    team_pids = {BLUE: [p["pid"] for p in ps if p["team"] == BLUE],
                 RED: [p["pid"] for p in ps if p["team"] == RED]}

    roam = {}
    for team_id in (BLUE, RED):
        sup = by_team_role.get((team_id, "UTILITY"))
        adc = by_team_role.get((team_id, "BOTTOM"))
        if not sup or not adc:
            continue
        away = total = 0
        for fr in frames:
            if _minute(fr.get("timestamp")) >= 14:
                continue
            sp, ap = _pframe(fr, sup).get("position"), _pframe(fr, adc).get("position")
            if sp and ap:
                total += 1
                if _dist(sp, ap) > 2800:
                    away += 1
        if total:
            roam[team_id] = round(away / total, 2)

    isolation = {}
    for p in ps:
        away = total = 0
        mates = [pid for pid in team_pids[p["team"]] if pid != p["pid"]]
        for fr in frames:
            if _minute(fr.get("timestamp")) < 15:
                continue
            pos = _pframe(fr, p["pid"]).get("position")
            if not pos:
                continue
            mate_pos = [_pframe(fr, m).get("position") for m in mates]
            dists = [_dist(pos, mp) for mp in mate_pos if mp]
            if dists:
                total += 1
                if min(dists) > 3500:
                    away += 1
        if total:
            isolation[p["pid"]] = {"champion": p["champion"], "role": p["role"],
                                   "team": p["team"], "isolated_ratio": round(away / total, 2)}
    return {"support_roam": roam, "isolation": isolation}


def build_macro_facts(match: dict, timeline: dict | None, focus_puuid: str | None = None) -> dict[str, Any]:
    """Assemble the full macro fact set for one game (both teams)."""
    info = match.get("info", {})
    ov = team_overview(match)
    focus_team = None
    if focus_puuid:
        focus_team = next((p["team"] for p in participants(match) if p["puuid"] == focus_puuid), None)
    return {
        "match_id": match.get("metadata", {}).get("matchId"),
        "duration_min": round((info.get("gameDuration", 0) or 0) / 60),
        "queue_id": info.get("queueId"),
        "focus_team": focus_team,
        "overview": ov,
        "objectives": objectives(match, timeline),
        "gold_xp": gold_xp_series(match, timeline),
        "vision": vision(match),
        "damage": damage_profiles(match),
        "lanes": lane_outcomes(match, timeline),
        "teamfights": teamfights(match, timeline),
        "deaths": death_analysis(match, timeline),
        "positions": positions(match, timeline),
    }
