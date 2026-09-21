"""Central configuration for the League AI Coach.

PRODUCT TENET
-------------
The coach exists to make players ready to climb in RANKED play. Both RANKED and
UNRANKED games are analyzed, but they play different roles:
  * RANKED   = the competitive arena that counts; performance here measures
               readiness for the next tier.
  * UNRANKED = practice. The ideal player drills skills in many unranked games
               so they transfer into ranked advancement.
So coaching frames unranked performance as practice, recommends what to drill in
unranked, and judges whether the player is practicing enough to climb. Skill
metrics are computed across BOTH; the BENCHMARK_TIER is the ranked tier the
player is preparing to reach.

Everything environment- or workspace-specific is read from env vars so that
nothing workspace-specific is hardcoded (a Databricks Apps requirement) and so
the same code runs locally, in the ingestion job, and in the deployed app.

Riot regional routing reference (https://developer.riotgames.com/docs/lol):
  * PLATFORM routing (na1, euw1, ...) -> summoner-v4, league-v4
  * REGION   routing (americas, ...)  -> account-v1, match-v5
"""

import os

# ---------------------------------------------------------------------------
# Target player & cohort
# ---------------------------------------------------------------------------
# The analyzed player is set per-request from the app's Riot ID / Region inputs
# (passed to the job as --game-name/--tag-line). These are only an optional
# fallback default; empty means there is no hardcoded player.
TARGET_GAME_NAME = os.environ.get("TARGET_GAME_NAME", "")
TARGET_TAG_LINE = os.environ.get("TARGET_TAG_LINE", "")

PLATFORM = os.environ.get("RIOT_PLATFORM", "na1")
REGION = os.environ.get("RIOT_REGION", "americas")

# All Riot regions. `code` doubles as the default tagLine (Riot sets a player's
# tagLine to their region code by default) and selects the API routing:
#   platform -> league-v4 / summoner-v4 host;  region -> account-v1 / match-v5.
REGIONS = [
    {"code": "NA1", "label": "North America", "platform": "na1", "region": "americas"},
    # NA2 is a custom tagLine, not a separate platform — route via NA (na1).
    {"code": "NA2", "label": "North America", "platform": "na1", "region": "americas"},
    {"code": "EUW1", "label": "EU West", "platform": "euw1", "region": "europe"},
    {"code": "EUN1", "label": "EU Nordic & East", "platform": "eun1", "region": "europe"},
    {"code": "KR", "label": "Korea", "platform": "kr", "region": "asia"},
    {"code": "JP1", "label": "Japan", "platform": "jp1", "region": "asia"},
    {"code": "BR1", "label": "Brazil", "platform": "br1", "region": "americas"},
    {"code": "LA1", "label": "Latin America North", "platform": "la1", "region": "americas"},
    {"code": "LA2", "label": "Latin America South", "platform": "la2", "region": "americas"},
    {"code": "OC1", "label": "Oceania", "platform": "oc1", "region": "sea"},
    {"code": "TR1", "label": "Turkey", "platform": "tr1", "region": "europe"},
    {"code": "RU", "label": "Russia", "platform": "ru", "region": "europe"},
    {"code": "PH2", "label": "Philippines", "platform": "ph2", "region": "sea"},
    {"code": "SG2", "label": "Singapore", "platform": "sg2", "region": "sea"},
    {"code": "TH2", "label": "Thailand", "platform": "th2", "region": "sea"},
    {"code": "TW2", "label": "Taiwan", "platform": "tw2", "region": "sea"},
    {"code": "VN2", "label": "Vietnam", "platform": "vn2", "region": "sea"},
]
_REGION_BY_CODE = {r["code"]: r for r in REGIONS}

# Tag/region prefixes -> (platform, regional routing). Lets a free-text Region
# (the tagLine, which can be custom — "NA1", "NA2", "KR1", "EUW", ...) still map
# to the correct Riot servers. Longest prefixes are matched first.
_TAG_ROUTING = {
    "NA": ("na1", "americas"), "BR": ("br1", "americas"),
    "LAN": ("la1", "americas"), "LAS": ("la2", "americas"),
    "LA1": ("la1", "americas"), "LA2": ("la2", "americas"),
    "EUW": ("euw1", "europe"), "EUNE": ("eun1", "europe"), "EUN": ("eun1", "europe"),
    "TR": ("tr1", "europe"), "RU": ("ru", "europe"),
    "KR": ("kr", "asia"), "JP": ("jp1", "asia"),
    "OCE": ("oc1", "sea"), "OC": ("oc1", "sea"), "PH": ("ph2", "sea"),
    "SG": ("sg2", "sea"), "TH": ("th2", "sea"), "TW": ("tw2", "sea"), "VN": ("vn2", "sea"),
}


def routing_for(code: str) -> tuple[str, str]:
    """(platform, region) routing for a region/tag string; falls back to NA.

    Tries an exact region-code match first, then a known tag prefix (so "KR1"
    routes to kr/asia, "NA2" to na1/americas, etc.). account-v1 is global, but
    match-v5 needs the right regional cluster, so this matters for fetching games.
    """
    c = (code or "").upper().strip()
    if c in _REGION_BY_CODE:
        r = _REGION_BY_CODE[c]
        return r["platform"], r["region"]
    for prefix in sorted(_TAG_ROUTING, key=len, reverse=True):
        if c.startswith(prefix):
            return _TAG_ROUTING[prefix]
    return "na1", "americas"

# The ranked tier the player is preparing to reach; we benchmark them against
# this cohort to measure readiness to climb.
BENCHMARK_TIER = os.environ.get("BENCHMARK_TIER", "GOLD")

# How many recent matches to pull per ingestion run (the app can override).
MATCH_FETCH_COUNT = int(os.environ.get("MATCH_FETCH_COUNT", "25"))

# Which games to pull: "ranked", "unranked", or "both" (the app can override).
QUEUE_MODE = os.environ.get("QUEUE_MODE", "both").lower()

# Maps a user-facing queue mode to the Riot match-v5 `type` categories to query.
# "both" queries ranked + normal (excludes ARAM/bot/tutorial noise) and merges.
_MATCH_TYPES = {
    "ranked": ["ranked"],
    "unranked": ["normal"],
    "both": ["ranked", "normal"],
}


def riot_match_types(mode: str | None = None) -> list[str]:
    """Riot match-v5 `type` values to query for a given queue mode."""
    return _MATCH_TYPES.get((mode or QUEUE_MODE).lower(), _MATCH_TYPES["both"])


# Queue-id classification, central to the tenet (ranked vs practice). Summoner's
# Rift normals count as ranked practice; ARAM/bots/rotating modes are "other".
RANKED_QUEUE_IDS = {420, 440}            # Solo/Duo, Flex
UNRANKED_QUEUE_IDS = {400, 430, 490}     # Draft, Blind, Quickplay (SR practice)


def queue_category(queue_id: int | None) -> str:
    """Map a Riot queueId to 'ranked', 'unranked' (SR practice), or 'other'."""
    if queue_id in RANKED_QUEUE_IDS:
        return "ranked"
    if queue_id in UNRANKED_QUEUE_IDS:
        return "unranked"
    return "other"


# Friendly mode names for display (the Recent Matches table). Not exhaustive —
# unknown ids show as "Other (<id>)".
QUEUE_NAMES = {
    400: "Normal Draft", 420: "Ranked Solo/Duo", 430: "Normal Blind",
    440: "Ranked Flex", 450: "ARAM", 490: "Quickplay", 700: "Clash",
    720: "ARAM Clash", 830: "Co-op vs AI", 840: "Co-op vs AI", 850: "Co-op vs AI",
    900: "ARURF", 1010: "Snow ARURF", 1020: "One for All", 1300: "Nexus Blitz",
    1400: "Ultimate Spellbook", 1700: "Arena", 1710: "Arena", 1750: "Arena",
    1900: "URF", 0: "Custom",
}


def queue_name(queue_id) -> str:
    """Human-readable game mode for a Riot queueId."""
    try:
        qid = int(queue_id)
    except (TypeError, ValueError):
        return "Unknown"
    return QUEUE_NAMES.get(qid, f"Other ({qid})")

# ---------------------------------------------------------------------------
# Riot API auth
# ---------------------------------------------------------------------------
# NEVER hardcode the key. Locally: export RIOT_API_KEY. In the app/job: injected
# from the Databricks secret declared in app.yaml / databricks.yml.
RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "")

# ---------------------------------------------------------------------------
# Unity Catalog destinations
# ---------------------------------------------------------------------------
UC_CATALOG = os.environ.get("UC_CATALOG", "main")
UC_SCHEMA = os.environ.get("UC_SCHEMA", "league_ai_coach")


def table(name: str) -> str:
    """Fully-qualified UC table name for a layer table."""
    return f"{UC_CATALOG}.{UC_SCHEMA}.{name}"


def summoner_table(game_name: str, region: str) -> str:
    """Per-player Delta table name, e.g. BaconAndEggsUSA + NA1 ->
    ccrawford.league_ai_coach.summoners_rift_baconandeggsusa_na1.

    Sanitizes gameName + region to a valid lowercase identifier (Riot IDs can
    contain spaces/punctuation). region doubles as the tagLine here.
    """
    import re

    name = re.sub(r"[^a-z0-9]", "", (game_name or "").lower()) or "unknown"
    reg = re.sub(r"[^a-z0-9]", "", (region or "").lower()) or "na1"
    return table(f"summoners_rift_{name}_{reg}")


# Medallion layout. One schema, layer-prefixed tables.
BRONZE_MATCHES = table("bronze_matches")               # raw match JSON, one row per match
SILVER_PARTICIPANTS = table("silver_match_participants")  # one row per participant per match
SILVER_PLAYER_RANKS = table("silver_player_ranks")     # cached league-v4 tier per puuid
GOLD_PLAYER_PERFORMANCE = table("gold_player_performance")  # target player's per-match metrics (incl. opponent tier)
GOLD_RANK_BENCHMARKS = table("gold_rank_benchmarks")   # per-tier/role benchmark aggregates
GOLD_CHALLENGE_BENCHMARKS = table("gold_challenge_benchmarks")  # tall: per-tier/role/metric averages


def set_target(game_name: str | None, tag_line: str | None) -> None:
    """Override the analyzed player at runtime (from the app's Riot ID inputs)."""
    global TARGET_GAME_NAME, TARGET_TAG_LINE
    TARGET_GAME_NAME = game_name or TARGET_GAME_NAME
    TARGET_TAG_LINE = tag_line or TARGET_TAG_LINE


def set_routing(platform: str | None, region: str | None) -> None:
    """Override Riot API routing at runtime (from the app's Region dropdown)."""
    global PLATFORM, REGION
    PLATFORM = platform or PLATFORM
    REGION = region or REGION


def set_destination(catalog: str | None, schema: str | None) -> None:
    """Repoint the UC destination at runtime and recompute table FQNs.

    Serverless job tasks can't take env vars, so the ingestion job passes the
    catalog/schema as CLI params and calls this before running the pipeline —
    keeping the job's writes aligned with the catalog the app reads from.
    """
    global UC_CATALOG, UC_SCHEMA
    global BRONZE_MATCHES, SILVER_PARTICIPANTS, SILVER_PLAYER_RANKS
    global GOLD_PLAYER_PERFORMANCE, GOLD_RANK_BENCHMARKS, GOLD_CHALLENGE_BENCHMARKS
    UC_CATALOG = catalog or UC_CATALOG
    UC_SCHEMA = schema or UC_SCHEMA
    BRONZE_MATCHES = table("bronze_matches")
    SILVER_PARTICIPANTS = table("silver_match_participants")
    SILVER_PLAYER_RANKS = table("silver_player_ranks")
    GOLD_PLAYER_PERFORMANCE = table("gold_player_performance")
    GOLD_RANK_BENCHMARKS = table("gold_rank_benchmarks")
    GOLD_CHALLENGE_BENCHMARKS = table("gold_challenge_benchmarks")

# league-v4 queueType the ranked Solo/Duo tier is read from.
RANKED_QUEUE_TYPE = os.environ.get("RANKED_QUEUE_TYPE", "RANKED_SOLO_5x5")

# ---------------------------------------------------------------------------
# Detailed coaching metrics: 20 high-signal fields from the match-v5
# `challenges` object. The Detailed Metrics view compares the player's per-game
# average of each to the GOLD cohort and asks the LLM what to improve.
#   key    -> Riot challenges key (also the silver/gold column name)
#   label  -> human label for the UI
#   better -> "high" (more is better) or "low" (deaths: less is better)
#   fmt    -> "pct" (0-1 fraction shown as %) or "num"
# ---------------------------------------------------------------------------
CHALLENGE_METRICS = [
    # Laning & economy
    {"key": "laneMinionsFirst10Minutes", "label": "CS @ 10 min", "better": "high", "fmt": "num"},
    {"key": "maxCsAdvantageOnLaneOpponent", "label": "Max CS lead vs lane", "better": "high", "fmt": "num"},
    {"key": "laningPhaseGoldExpAdvantage", "label": "Laning gold/XP adv", "better": "high", "fmt": "num"},
    {"key": "earlyLaningPhaseGoldExpAdvantage", "label": "Early laning adv", "better": "high", "fmt": "num"},
    {"key": "goldPerMinute", "label": "Gold / min", "better": "high", "fmt": "num"},
    # Combat & impact
    {"key": "kda", "label": "KDA", "better": "high", "fmt": "num"},
    {"key": "killParticipation", "label": "Kill participation", "better": "high", "fmt": "pct"},
    {"key": "deathsByEnemyChamps", "label": "Deaths", "better": "low", "fmt": "num"},
    {"key": "soloKills", "label": "Solo kills", "better": "high", "fmt": "num"},
    {"key": "takedownsFirstXMinutes", "label": "Early takedowns", "better": "high", "fmt": "num"},
    {"key": "damagePerMinute", "label": "Damage / min", "better": "high", "fmt": "num"},
    {"key": "teamDamagePercentage", "label": "Team damage share", "better": "high", "fmt": "pct"},
    # Mechanics
    {"key": "skillshotsHit", "label": "Skillshots hit", "better": "high", "fmt": "num"},
    {"key": "skillshotsDodged", "label": "Skillshots dodged", "better": "high", "fmt": "num"},
    # Vision
    {"key": "visionScorePerMinute", "label": "Vision score / min", "better": "high", "fmt": "num"},
    {"key": "visionScoreAdvantageLaneOpponent", "label": "Vision adv vs lane", "better": "high", "fmt": "num"},
    {"key": "controlWardsPlaced", "label": "Control wards placed", "better": "high", "fmt": "num"},
    {"key": "wardTakedowns", "label": "Wards cleared", "better": "high", "fmt": "num"},
    # Objectives
    {"key": "dragonTakedowns", "label": "Dragon takedowns", "better": "high", "fmt": "num"},
    {"key": "turretPlatesTaken", "label": "Turret plates taken", "better": "high", "fmt": "num"},
]
CHALLENGE_KEYS = [m["key"] for m in CHALLENGE_METRICS]

# ---------------------------------------------------------------------------
# Databricks resources (injected as env vars in the app via `valueFrom`)
# ---------------------------------------------------------------------------
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
# FMAPI chat endpoint name, e.g. "databricks-claude-3-7-sonnet".
SERVING_ENDPOINT = os.environ.get("SERVING_ENDPOINT", "")
# Lakeflow job id the app triggers for an on-demand refresh.
INGEST_JOB_ID = os.environ.get("INGEST_JOB_ID", "")
