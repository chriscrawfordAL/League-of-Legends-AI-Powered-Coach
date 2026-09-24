"""Spark Declarative Pipeline — rebuild the tier/role benchmark medallion.

Runs weekly (via the ingest job's pipeline task) to refresh the cohort benchmark
reference data the coach grades against. Bronze fetches a cohort of ranked players
across tiers from Riot (imperative, cached so it runs once per process); silver +
gold are declarative materialized views. Config comes from the pipeline
`configuration`: league.tiers, league.sample_size, league.matches_per_player.

Dataset functions stay LAZY (no eager .collect()/raise) so SDP's flow-analysis
phase — which runs the functions before upstreams are materialized — succeeds; the
Python row-shaper (participant_rows) is applied via a UDF + explode instead of a
driver-side collect.
"""

import json
import os
import sys

from pyspark import pipelines as dp
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType


def _add_src() -> None:
    """Put the project's src/ on the path so we can reuse config + the Riot client
    + row shaping (avoids duplicating participant_rows / the 20-metric coercion)."""
    starts = []
    try:
        starts.append(os.path.dirname(os.path.abspath(__file__)))  # .../files/pipelines
    except NameError:
        pass
    starts.append(os.getcwd())
    for start in starts:
        d = start
        for _ in range(8):
            cand = os.path.join(d, "src")
            if os.path.exists(os.path.join(cand, "config.py")):
                if cand not in sys.path:
                    sys.path.insert(0, cand)
                return
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent


_add_src()

import config  # noqa: E402
from ingest.pipeline import fetch_cohort_rows  # noqa: E402
from riot.client import RiotClient  # noqa: E402
from riot.models import participant_rows  # noqa: E402

spark = SparkSession.getActiveSession()

_TIERS = [t.strip() for t in spark.conf.get(
    "league.tiers", "IRON,BRONZE,SILVER,GOLD,PLATINUM,EMERALD,DIAMOND").split(",") if t.strip()]
_SAMPLE = int(spark.conf.get("league.sample_size", "25"))
_MPP = int(spark.conf.get("league.matches_per_player", "4"))

_BRONZE_SCHEMA = ("match_id string, cohort_puuid string, cohort_tier string, "
                  "ingested_at bigint, payload string")

# Derive the participant struct schema from a synthetic match (challenge metrics
# populated so numeric columns infer DoubleType, not NullType) — keeps the UDF
# schema in lock-step with participant_rows without hand-listing 40+ columns.
_SYNTH_MATCH = {
    "metadata": {"matchId": "SYNTH"},
    "info": {
        "gameDuration": 1800, "queueId": 420, "gameCreation": 0,
        "participants": [{
            "puuid": "p", "teamId": 100, "win": True, "championName": "c",
            "teamPosition": "BOTTOM", "kills": 0, "deaths": 0, "assists": 0,
            "goldEarned": 0, "visionScore": 0, "totalDamageDealtToChampions": 0,
            "totalMinionsKilled": 0, "neutralMinionsKilled": 0,
            "riotIdGameName": "g", "riotIdTagline": "t",
            "challenges": {k: 0.0 for k in config.CHALLENGE_KEYS} | {"killParticipation": 0.0},
        }],
    },
}
_PART_SCHEMA = spark.createDataFrame(participant_rows(_SYNTH_MATCH)).schema

# Fetch the cohort once per process (bronze's function may be invoked at both
# analysis and materialization time).
_fetch_cache: dict = {}


def _cohort_rows() -> list:
    if "rows" not in _fetch_cache:
        from pyspark.dbutils import DBUtils

        key = DBUtils(spark).secrets.get(scope="league_ai_coach", key="riot_api_key")
        client = RiotClient(api_key=key, platform=config.PLATFORM, region=config.REGION)
        _fetch_cache["rows"] = fetch_cohort_rows(
            client, _TIERS, sample_size=_SAMPLE, matches_per_player=_MPP)
    return _fetch_cache["rows"]


@F.udf(returnType=ArrayType(_PART_SCHEMA))
def _flatten(payload):
    return participant_rows(json.loads(payload)) if payload else []


@dp.materialized_view()
def bronze_matches():
    """Cohort match payloads sampled from Riot across tiers (imperative fetch)."""
    rows = _cohort_rows()
    if not rows:
        return spark.createDataFrame([], _BRONZE_SCHEMA)
    return spark.createDataFrame(rows)


@dp.materialized_view()
def silver_player_ranks():
    """Each cohort player's known tier — derived from bronze (no rank lookups)."""
    return (spark.read.table("bronze_matches")
            .select(F.col("cohort_puuid").alias("puuid"),
                    F.col("cohort_tier").alias("tier"))
            .where(F.col("puuid").isNotNull()).distinct())


@dp.materialized_view()
def silver_match_participants():
    """One row per participant per match (flattened via the shared row shaper,
    applied lazily as a UDF + explode so SDP analysis doesn't force a collect)."""
    return (spark.read.table("bronze_matches")
            .dropDuplicates(["match_id"])
            .select(F.explode(_flatten(F.col("payload"))).alias("p"))
            .select("p.*"))


def _cohort_joined():
    silver = spark.read.table("silver_match_participants")
    ranks = spark.read.table("silver_player_ranks").select("puuid", "tier")
    return (silver.join(ranks, "puuid", "inner")
            .where(F.col("tier").isNotNull())
            .where(F.col("team_position") != ""))


@dp.materialized_view()
def gold_rank_benchmarks():
    """Per-(tier, role) cohort averages of the headline coaching metrics."""
    return _cohort_joined().groupBy("tier", "team_position").agg(
        F.avg("cs_per_min").alias("cs_per_min"),
        (F.avg(F.col("kills") + F.col("assists"))
         / F.greatest(F.avg("deaths"), F.lit(1.0))).alias("kda"),
        F.avg("vision_per_min").alias("vision_per_min"),
        F.avg("gold_per_min").alias("gold_per_min"),
        F.avg("kill_participation").alias("kill_participation"),
        F.count("*").alias("sample_size"))


@dp.materialized_view()
def gold_challenge_benchmarks():
    """Tall per-(tier, role, metric) cohort averages for the 20 challenge metrics."""
    keys = config.CHALLENGE_KEYS
    agg = _cohort_joined().groupBy("tier", "team_position").agg(
        F.count("*").alias("sample_size"),
        *[F.avg(F.col(k)).alias(k) for k in keys])
    pairs = ", ".join(f"'{k}', `{k}`" for k in keys)
    return agg.selectExpr("tier", "team_position", "sample_size",
                          f"stack({len(keys)}, {pairs}) as (metric, gold_avg)")
