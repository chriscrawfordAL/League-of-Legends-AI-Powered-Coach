"""Tests for deterministic macro (team/game-level) analysis. No network."""

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from analysis import macro  # noqa: E402

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def _p(pid, team, role, champ, **kw):
    base = dict(participantId=pid, teamId=team, teamPosition=role, championName=champ,
                puuid=f"P{pid}", win=(team == 100), kills=2, deaths=2, assists=4,
                totalMinionsKilled=120, neutralMinionsKilled=0, goldEarned=10000,
                visionScore=20, wardsPlaced=10, wardsKilled=2, detectorWardsPlaced=2,
                physicalDamageDealtToChampions=5000, magicDamageDealtToChampions=5000,
                trueDamageDealtToChampions=0, totalDamageDealtToChampions=10000,
                totalDamageTaken=12000, timeCCingOthers=20, champLevel=14)
    for i in range(7):
        base[f"item{i}"] = 0
    base.update(kw)
    return base


def _match():
    parts = []
    champs = {100: ["Ornn", "LeeSin", "Syndra", "Jinx", "Thresh"],
              200: ["Sett", "Elise", "Zed", "Caitlyn", "Lulu"]}
    for team in (100, 200):
        for role, champ in zip(ROLES, champs[team]):
            pid = (1 if team == 100 else 6) + ROLES.index(role)
            parts.append(_p(pid, team, role, champ))
    # Make MIDDLEs AP threats, BOTTOMs AD threats.
    by = {(p["teamId"], p["teamPosition"]): p for p in parts}
    by[(100, "MIDDLE")].update(magicDamageDealtToChampions=20000, physicalDamageDealtToChampions=1000,
                               totalDamageDealtToChampions=21000)
    by[(100, "BOTTOM")].update(physicalDamageDealtToChampions=25000, magicDamageDealtToChampions=500,
                               totalDamageDealtToChampions=25500)
    by[(200, "MIDDLE")].update(magicDamageDealtToChampions=18000, physicalDamageDealtToChampions=800,
                               totalDamageDealtToChampions=18800)

    def obj(fb, tw, twf, dr, drf, rh, ba, inh):
        return {"champion": {"first": fb, "kills": 20}, "tower": {"first": twf, "kills": tw},
                "dragon": {"first": drf, "kills": dr}, "riftHerald": {"first": rh, "kills": 1 if rh else 0},
                "baron": {"first": ba, "kills": 1 if ba else 0},
                "inhibitor": {"first": False, "kills": inh}, "horde": {"first": False, "kills": 3}}

    return {"metadata": {"matchId": "NA1_TEST"}, "info": {
        "gameDuration": 1800, "queueId": 420, "participants": parts,
        "teams": [
            {"teamId": 100, "win": True, "bans": [],
             "objectives": obj(True, 8, True, 3, True, True, True, 1)},
            {"teamId": 200, "win": False, "bans": [],
             "objectives": obj(False, 2, False, 1, False, False, False, 0)},
        ]}}


def _pf(pid, gold, xp, cs, x=7000, y=7000):
    return {"participantId": pid, "totalGold": gold, "xp": xp, "minionsKilled": cs,
            "jungleMinionsKilled": 0, "position": {"x": x, "y": y}}


def _timeline():
    def frame(ts, blue_gold, red_gold, events=None):
        pf = {}
        for pid in range(1, 11):
            team_gold = blue_gold if pid <= 5 else red_gold
            # give MIDDLEs (pid 3 / 8) distinct gold for lane_outcomes @14
            g = 8000 if pid == 3 else (7000 if pid == 8 else team_gold)
            pf[str(pid)] = _pf(pid, g, g, g // 100)
        return {"timestamp": ts, "participantFrames": pf, "events": events or []}

    frames = [
        frame(0, 500, 500),
        frame(600000, 6000, 5000),
        frame(840000, 9000, 8000),   # 14 min — lane outcomes mark
        frame(900000, 10000, 8500, events=[  # a teamfight at 15min: 2 red + 1 blue die
            {"type": "CHAMPION_KILL", "timestamp": 900000, "killerId": 1, "victimId": 6},
            {"type": "CHAMPION_KILL", "timestamp": 905000, "killerId": 2, "victimId": 7},
            {"type": "CHAMPION_KILL", "timestamp": 910000, "killerId": 8, "victimId": 3},
        ]),
        frame(1200000, 15000, 11000, events=[
            {"type": "ELITE_MONSTER_KILL", "timestamp": 1200000, "killerTeamId": 100,
             "monsterType": "DRAGON", "monsterSubType": "FIRE_DRAGON"},
            {"type": "DRAGON_SOUL_GIVEN", "timestamp": 1200500, "teamId": 100},
            {"type": "BUILDING_KILL", "timestamp": 1201000, "teamId": 200,
             "buildingType": "TOWER_BUILDING", "laneType": "MID_LANE"},
        ]),
    ]
    return {"info": {"frames": frames}}


def test_team_overview():
    ov = macro.team_overview(_match())
    assert ov[100]["win"] is True and ov[200]["win"] is False
    assert ov[100]["champions"]["MIDDLE"] == "Syndra"
    assert ov[100]["kills"] == 10  # 5 members * 2 kills


def test_objectives_counts_and_soul():
    o = macro.objectives(_match(), _timeline())
    assert o[100]["dragons"] == 3 and o[100]["first_dragon"] is True
    assert o[100]["first_blood"] is True and o[200]["first_blood"] is False
    assert o[100]["barons"] == 1 and o[100]["soul"] is True
    assert "Fire" in o[100]["dragon_types"]
    # building credited to the taker (blue took red's mid tower)
    assert any(e["type"] == "TOWER_BUILDING" and e["team"] == 100 for e in o["events"])


def test_damage_profiles_threats():
    d = macro.damage_profiles(_match())
    assert d[100]["biggest_ap"]["champion"] == "Syndra"   # 20k magic
    assert d[100]["biggest_ad"]["champion"] == "Jinx"      # 25k phys
    assert d[200]["biggest_ap"]["champion"] == "Zed"       # 18k magic


def test_gold_xp_series_summary():
    g = macro.gold_xp_series(_match(), _timeline())
    assert g["series"][2]["minute"] == 14
    assert g["summary"]["final_gold_diff"] > 0      # blue ahead at end
    assert g["summary"]["threw_lead"] is False


def test_lane_outcomes_mid():
    lanes = macro.lane_outcomes(_match(), _timeline())
    mid = next(l for l in lanes if l["role"] == "MIDDLE")
    assert mid["gold_diff"] == 1000 and mid["winner"] == "Blue"  # 8000 - 7000


def test_teamfights_cluster():
    tf = macro.teamfights(_match(), _timeline())
    assert tf["summary"]["total"] == 1
    assert tf["fights"][0]["blue_kills"] == 2 and tf["fights"][0]["red_kills"] == 1
    assert tf["fights"][0]["result"] == "Blue"


def test_death_analysis():
    d = macro.death_analysis(_match(), _timeline())
    assert d[100]["deaths"] == 1 and d[200]["deaths"] == 2


def test_build_macro_facts_focus():
    facts = macro.build_macro_facts(_match(), _timeline(), focus_puuid="P3")
    assert facts["focus_team"] == 100
    assert facts["duration_min"] == 30
    assert set(facts) >= {"overview", "objectives", "gold_xp", "vision", "damage",
                          "lanes", "teamfights", "deaths", "positions"}
