"""Medallion ingestion: Riot API -> bronze -> silver -> gold (Unity Catalog).

Runs inside a Databricks job where a SparkSession and Unity Catalog write access
are available. The functions take an explicit SparkSession + RiotClient so they
stay unit-testable and contain no module-level side effects.

Flow
----
1. Resolve the target player's puuid (account-v1).
2. Pull recent match ids (match-v5) and the full match payloads -> BRONZE.
3. Flatten participants -> SILVER_PARTICIPANTS.
4. Look up + cache each distinct participant's ranked tier (league-v4)
   -> SILVER_PLAYER_RANKS.
5. Project the target player's per-match metrics, attaching the LANE OPPONENT's
   tier -> GOLD_PLAYER_PERFORMANCE (lets the app isolate games vs GOLD players).
6. Aggregate cohort benchmarks by (tier, role) -> GOLD_RANK_BENCHMARKS.
"""

from __future__ import annotations

import json
import random
import time

import config
from riot.client import RiotAPIError, RiotClient
from riot.models import participant_rows, rank_row

DIVISIONS = ("I", "II", "III", "IV")

_BENCH_METRICS = ["cs_per_min", "kda", "vision_per_min", "gold_per_min", "kill_participation"]


_MATCH_IDS_PAGE = 100  # match-v5 returns at most 100 ids per request


def _paged_match_ids(client, puuid, count, type_, start_time, end_time) -> list[str]:
    """Fetch up to ``count`` match ids, paging past the 100-per-request cap.

    Stops early when a page returns fewer than requested (no more history).
    """
    ids: list[str] = []
    start = 0
    while len(ids) < count:
        page = client.get_match_ids(
            puuid, start=start, count=min(_MATCH_IDS_PAGE, count - len(ids)),
            type_=type_, start_time=start_time, end_time=end_time,
        )
        if not page:
            break
        ids.extend(page)
        if len(page) < _MATCH_IDS_PAGE:
            break  # last page
        start += len(page)
    return ids[:count]


def ensure_schema(spark) -> None:
    # The catalog is assumed to exist (provisioning a catalog needs a managed
    # location and is an admin/setup concern, not the pipeline's). We only
    # ensure the target schema within it.
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config.UC_CATALOG}.{config.UC_SCHEMA}")


def fetch_to_bronze(
    spark,
    client: RiotClient,
    queue_mode: str | None = None,
    count: int | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    fresh: bool = True,
) -> int:
    """Pull the target player's matches into the bronze table.

    ``queue_mode`` is "ranked", "unranked", or "both"; ``count`` bounds how many
    of the most-recent matches to keep; ``start_time``/``end_time`` are epoch
    seconds for an optional timeframe. For "both" we query ranked + normal and
    keep the ``count`` most recent by game creation.

    ``fresh`` selects the refresh mode:
      * fresh=True  (FRESH BACKFILL) — delete the player's previously-ingested
        matches first, then re-pull the full requested window, so the analysis
        reflects exactly the requested timeframe/count with no stale carryover.
      * fresh=False (INCREMENTAL) — keep existing matches and only append
        newly-seen ones (faster; fewer API calls).
    Either way the wipe/dedup is scoped to the player; other players' rows (the
    benchmark cohort) are left intact. Returns matches written for the player.
    """
    queue_mode = queue_mode or config.QUEUE_MODE
    count = count or config.MATCH_FETCH_COUNT
    account = client.get_account_by_riot_id(
        config.TARGET_GAME_NAME, config.TARGET_TAG_LINE
    )
    puuid = account["puuid"]

    # Fresh backfill clears this player's prior matches (scoped to their puuid so
    # the cohort/benchmark rows survive); incremental keeps them and appends.
    if fresh and spark.catalog.tableExists(config.BRONZE_MATCHES):
        spark.sql(f"DELETE FROM {config.BRONZE_MATCHES} WHERE puuid = '{puuid}'")

    types = config.riot_match_types(queue_mode)
    # Dedup-preserving union of ids across the requested type(s), paginating past
    # the match-v5 per-request cap of 100 to honor large `count` values.
    candidate_ids: list[str] = []
    seen_ids: set[str] = set()
    for t in types:
        for mid in _paged_match_ids(client, puuid, count, t, start_time, end_time):
            if mid not in seen_ids:
                seen_ids.add(mid)
                candidate_ids.append(mid)

    existing = _existing_match_ids(spark)
    fetched = []  # (game_creation, match_id, payload)
    for match_id in candidate_ids:
        if match_id in existing:
            continue
        match = client.get_match(match_id)
        fetched.append(
            (match.get("info", {}).get("gameCreation", 0), match_id, json.dumps(match))
        )

    # Keep the most-recent `count` (matters when "both" merges two id lists).
    fetched.sort(key=lambda x: x[0], reverse=True)
    now = int(time.time())
    rows = [
        {"match_id": mid, "puuid": puuid, "ingested_at": now, "payload": payload}
        for _, mid, payload in fetched[:count]
    ]

    if rows:
        spark.createDataFrame(rows).write.mode("append").saveAsTable(
            config.BRONZE_MATCHES
        )
    return len(rows)


def build_summoner_table(spark, client, game_name, tag_line, region,
                         count=None, start_time=None, queue_mode="both") -> dict:
    """Fetch a player's full window from Riot and write their per-player table.

    Writes one row per match (the player's own row) to
    ``config.summoner_table(game_name, region)``, overwriting it. This is the
    background backfill the app triggers for windows > 30 days; the table is the
    persistent store the app reads on reload.
    """
    from itertools import zip_longest

    count = count or config.MATCH_FETCH_COUNT
    account = client.get_account_by_riot_id(game_name, tag_line)
    puuid = account["puuid"]

    if queue_mode == "both":
        ranked = _paged_match_ids(client, puuid, count, "ranked", start_time, None)
        normal = _paged_match_ids(client, puuid, count, "normal", start_time, None)
        ids, seen = [], set()
        for a, b in zip_longest(ranked, normal):
            for x in (a, b):
                if x and x not in seen:
                    seen.add(x)
                    ids.append(x)
        ids = ids[:count]
    else:
        type_ = {"ranked": "ranked", "unranked": "normal"}.get(queue_mode)
        ids = _paged_match_ids(client, puuid, count, type_, start_time, None)

    rows = []
    for mid in ids:
        try:
            match = client.get_match(mid)
        except RiotAPIError:
            continue
        for r in participant_rows(match):
            if r["puuid"] == puuid:
                rows.append(r)
                break

    tbl = config.summoner_table(game_name, region)
    if rows:
        spark.createDataFrame(rows).write.mode("overwrite").option(
            "overwriteSchema", "true").saveAsTable(tbl)
    return {"summoner_table": tbl, "matches": len(rows)}


def ingest_cohort(
    spark,
    client: RiotClient,
    tier: str = "GOLD",
    sample_size: int = 20,
    matches_per_player: int = 3,
    seed: int = 13,
) -> dict:
    """Seed bronze with matches from a sampled cohort of ``tier`` players.

    Discovers players via league-v4 entries (no usernames needed), samples
    ``sample_size`` of them, and pulls their recent ranked matches into bronze.
    Because we KNOW these seed players' tier, we also write them straight into
    SILVER_PLAYER_RANKS — so ``build_gold_benchmarks`` aggregates a clean
    cohort benchmark from their own performance rows without per-participant
    rank lookups.
    """
    rng = random.Random(seed)
    queue = 420  # cohort benchmarks come from ranked Solo/Duo for a clean per-tier signal

    # Collect candidate puuids across divisions (one page each is ~205 players).
    candidates: dict[str, dict] = {}
    for division in DIVISIONS:
        for entry in client.get_league_entries_by_tier(tier, division, page=1):
            puuid = entry.get("puuid")
            if puuid:
                candidates[puuid] = entry
    sampled = rng.sample(list(candidates), k=min(sample_size, len(candidates)))

    # Record their known ranks (skip ones already cached).
    cached = _cached_rank_puuids(spark)
    rank_rows = [
        {
            "puuid": p,
            "queue_type": config.RANKED_QUEUE_TYPE,
            "tier": candidates[p].get("tier"),
            "division": candidates[p].get("rank"),
            "league_points": candidates[p].get("leaguePoints"),
        }
        for p in sampled
        if p not in cached
    ]
    if rank_rows:
        spark.createDataFrame(rank_rows).write.mode("append").saveAsTable(
            config.SILVER_PLAYER_RANKS
        )

    # Pull their recent matches into bronze (dedup against what's there).
    existing = _existing_match_ids(spark)
    new_bronze: list[dict] = []
    for puuid in sampled:
        try:
            ids = client.get_match_ids(puuid, count=matches_per_player, queue=queue)
        except RiotAPIError:
            continue
        for mid in ids:
            if mid in existing or any(r["match_id"] == mid for r in new_bronze):
                continue
            try:
                match = client.get_match(mid)
            except RiotAPIError:
                continue
            new_bronze.append(
                {
                    "match_id": mid,
                    "puuid": puuid,
                    "ingested_at": int(time.time()),
                    "payload": json.dumps(match),
                }
            )

    if new_bronze:
        spark.createDataFrame(new_bronze).write.mode("append").saveAsTable(
            config.BRONZE_MATCHES
        )
    return {"sampled_players": len(sampled), "new_matches": len(new_bronze)}


def fetch_cohort_rows(client, tiers, sample_size: int = 25,
                      matches_per_player: int = 4, seed: int = 13) -> list[dict]:
    """Pure cohort fetch for the declarative pipeline — no Spark, no writes.

    For each tier: sample ``sample_size`` ranked players via league-v4, pull their
    recent ranked matches, and return bronze rows tagged with the sampled player's
    puuid + KNOWN tier. Tagging the cohort tier on each row lets the pipeline derive
    silver_player_ranks straight from bronze (no per-participant rank lookups).
    """
    rng = random.Random(seed)
    queue = 420  # ranked Solo/Duo — clean per-tier signal
    rows: list[dict] = []
    seen: set[str] = set()
    for tier in tiers:
        candidates: dict[str, dict] = {}
        for division in DIVISIONS:
            try:
                for entry in client.get_league_entries_by_tier(tier, division, page=1):
                    p = entry.get("puuid")
                    if p:
                        candidates[p] = entry
            except RiotAPIError:
                continue
        if not candidates:
            continue
        sampled = rng.sample(list(candidates), k=min(sample_size, len(candidates)))
        for puuid in sampled:
            try:
                ids = client.get_match_ids(puuid, count=matches_per_player, queue=queue)
            except RiotAPIError:
                continue
            for mid in ids:
                if mid in seen:
                    continue
                seen.add(mid)
                try:
                    match = client.get_match(mid)
                except RiotAPIError:
                    continue
                rows.append({
                    "match_id": mid,
                    "cohort_puuid": puuid,
                    "cohort_tier": tier,
                    "ingested_at": int(time.time()),
                    "payload": json.dumps(match),
                })
    return rows


def build_silver(spark) -> None:
    """Flatten bronze match payloads into one row per participant.

    Dedups bronze by match_id first, so a match flattens exactly once even if it
    were ever stored under more than one puuid (defends against duplicate
    participant rows downstream).
    """
    bronze = spark.read.table(config.BRONZE_MATCHES).collect()
    all_rows: list[dict] = []
    seen_matches: set[str] = set()
    for r in bronze:
        if r["match_id"] in seen_matches:
            continue
        seen_matches.add(r["match_id"])
        all_rows.extend(participant_rows(json.loads(r["payload"])))
    if not all_rows:
        return
    spark.createDataFrame(all_rows).write.mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(config.SILVER_PARTICIPANTS)


def build_player_ranks(spark, client: RiotClient) -> int:
    """Look up + cache the ranked tier of every participant seen in silver.

    Only puuids not already cached are fetched, so re-runs are cheap and stay
    within Riot rate limits. Unranked players are still recorded (tier=None) so
    they are not re-queried every run. Returns the number of newly fetched puuids.
    """
    if not spark.catalog.tableExists(config.SILVER_PARTICIPANTS):
        return 0

    seen = {
        r["puuid"]
        for r in spark.read.table(config.SILVER_PARTICIPANTS).select("puuid").distinct().collect()
        if r["puuid"]
    }
    cached = _cached_rank_puuids(spark)
    todo = sorted(seen - cached)

    new_rows: list[dict] = []
    for puuid in todo:
        try:
            entries = client.get_league_entries_by_puuid(puuid)
        except RiotAPIError:
            # Skip a single bad lookup rather than fail the whole run.
            continue
        new_rows.append(rank_row(puuid, entries, config.RANKED_QUEUE_TYPE))

    if new_rows:
        spark.createDataFrame(new_rows).write.mode("append").saveAsTable(
            config.SILVER_PLAYER_RANKS
        )
    return len(new_rows)


def build_gold_player(spark, limit: int | None = None) -> None:
    """Target player's per-match performance, with the lane opponent's tier.

    The lane opponent is the enemy participant (different team_id) sharing the
    same team_position in the same match. Joining their cached tier lets the app
    filter to games played against GOLD-level players. ``limit`` bounds the
    output to the most-recent N matches (defaults to MATCH_FETCH_COUNT) so the
    analysis window matches the "go back N games" pull setting even though
    bronze accumulates more over time.
    """
    from pyspark.sql import functions as F

    limit = limit or config.MATCH_FETCH_COUNT
    silver = spark.read.table(config.SILVER_PARTICIPANTS)
    player = silver.filter(F.col("riot_id_game_name") == config.TARGET_GAME_NAME)
    # Keep only the most-recent `limit` matches for the player.
    recent_ids = [
        r["match_id"]
        for r in player.select("match_id", "game_creation")
        .distinct()
        .orderBy(F.col("game_creation").desc())
        .limit(limit)
        .collect()
    ]
    player = player.filter(F.col("match_id").isin(recent_ids))

    opponents = silver.select(
        F.col("match_id").alias("o_match_id"),
        F.col("team_position").alias("o_team_position"),
        F.col("team_id").alias("o_team_id"),
        F.col("puuid").alias("opponent_puuid"),
    )
    # Only match a lane opponent when the player HAS a lane (Summoner's Rift).
    # Modes without lanes (Arena/ARAM) have empty team_position, which would
    # otherwise fan out to many "opponents" and duplicate the match row.
    joined = player.join(
        opponents,
        (player.match_id == opponents.o_match_id)
        & (player.team_position == opponents.o_team_position)
        & (player.team_id != opponents.o_team_id)
        & (player.team_position.isNotNull())
        & (player.team_position != ""),
        how="left",
    )

    if spark.catalog.tableExists(config.SILVER_PLAYER_RANKS):
        ranks = spark.read.table(config.SILVER_PLAYER_RANKS).select(
            F.col("puuid").alias("r_puuid"), F.col("tier").alias("opponent_tier")
        )
        joined = joined.join(
            ranks, joined.opponent_puuid == ranks.r_puuid, how="left"
        ).drop("r_puuid")
    else:
        joined = joined.withColumn("opponent_tier", F.lit(None).cast("string"))

    joined.drop("o_match_id", "o_team_position", "o_team_id").write.mode(
        "overwrite"
    ).option("overwriteSchema", "true").saveAsTable(config.GOLD_PLAYER_PERFORMANCE)


def build_gold_benchmarks(spark) -> None:
    """Per-(tier, role) cohort averages over every ranked participant seen.

    Joins silver participants to their cached tier and averages the coaching
    metrics grouped by (tier, team_position). The app reads the rows for the
    benchmark tier (e.g. GOLD) instead of the static fallback in metrics.py.
    """
    from pyspark.sql import functions as F

    if not spark.catalog.tableExists(config.SILVER_PLAYER_RANKS):
        return

    silver = spark.read.table(config.SILVER_PARTICIPANTS)
    ranks = spark.read.table(config.SILVER_PLAYER_RANKS).select("puuid", "tier")
    joined = (
        silver.join(ranks, "puuid", "inner")
        .filter(F.col("tier").isNotNull())
        .filter(F.col("team_position") != "")
    )

    agg = joined.groupBy("tier", "team_position").agg(
        F.avg("cs_per_min").alias("cs_per_min"),
        (F.avg(F.col("kills") + F.col("assists")) / F.greatest(F.avg("deaths"), F.lit(1.0))).alias("kda"),
        F.avg("vision_per_min").alias("vision_per_min"),
        F.avg("gold_per_min").alias("gold_per_min"),
        F.avg("kill_participation").alias("kill_participation"),
        F.count("*").alias("sample_size"),
    )
    agg.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(
        config.GOLD_RANK_BENCHMARKS
    )


def build_challenge_benchmarks(spark) -> None:
    """Tall per-(tier, role, metric) cohort averages for the 20 challenge metrics.

    Powers the Detailed Metrics view: one row per (tier, team_position, metric)
    with the cohort's average value, so the app can show the GOLD average next to
    the player's value for each metric. Long format keeps it flexible as the
    metric catalog evolves.
    """
    from pyspark.sql import functions as F

    if not spark.catalog.tableExists(config.SILVER_PLAYER_RANKS):
        return

    silver = spark.read.table(config.SILVER_PARTICIPANTS)
    ranks = spark.read.table(config.SILVER_PLAYER_RANKS).select("puuid", "tier")
    joined = (
        silver.join(ranks, "puuid", "inner")
        .filter(F.col("tier").isNotNull())
        .filter(F.col("team_position") != "")
    )

    keys = config.CHALLENGE_KEYS
    agg = joined.groupBy("tier", "team_position").agg(
        F.count("*").alias("sample_size"),
        *[F.avg(F.col(k)).alias(k) for k in keys],
    )
    # Unpivot the per-metric columns into (metric, gold_avg) rows.
    pairs = ", ".join(f"'{k}', `{k}`" for k in keys)
    stack_expr = f"stack({len(keys)}, {pairs}) as (metric, gold_avg)"
    tall = agg.selectExpr("tier", "team_position", "sample_size", stack_expr)
    tall.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(
        config.GOLD_CHALLENGE_BENCHMARKS
    )


def _existing_match_ids(spark) -> set[str]:
    if not spark.catalog.tableExists(config.BRONZE_MATCHES):
        return set()
    rows = spark.read.table(config.BRONZE_MATCHES).select("match_id").collect()
    return {r["match_id"] for r in rows}


def _cached_rank_puuids(spark) -> set[str]:
    if not spark.catalog.tableExists(config.SILVER_PLAYER_RANKS):
        return set()
    rows = spark.read.table(config.SILVER_PLAYER_RANKS).select("puuid").collect()
    return {r["puuid"] for r in rows}


def run(
    spark,
    client: RiotClient,
    queue_mode: str | None = None,
    count: int | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    fresh: bool = True,
) -> dict:
    """Run the full pipeline. Returns a small summary dict for logging.

    Pull controls (``queue_mode``/``count``/``start_time``/``end_time``/``fresh``)
    come from the app's toggles via the job; they default to config values.
    """
    count = count or config.MATCH_FETCH_COUNT
    ensure_schema(spark)
    new_matches = fetch_to_bronze(
        spark, client, queue_mode=queue_mode, count=count,
        start_time=start_time, end_time=end_time, fresh=fresh,
    )
    build_silver(spark)
    new_ranks = build_player_ranks(spark, client)
    build_gold_player(spark, limit=count)
    build_gold_benchmarks(spark)
    build_challenge_benchmarks(spark)
    return {"new_matches": new_matches, "new_ranks": new_ranks}


def run_cohort(spark, client: RiotClient, tier: str = "GOLD", sample_size: int = 20) -> dict:
    """One-off: seed a single tier cohort and (re)build the benchmark tables."""
    ensure_schema(spark)
    summary = ingest_cohort(spark, client, tier=tier, sample_size=sample_size)
    build_silver(spark)
    build_gold_benchmarks(spark)
    build_challenge_benchmarks(spark)
    return summary


# Tiers that use the standard league-v4 entries-by-division endpoint (Iron→Diamond;
# apex tiers Master+ use different endpoints and aren't offered in the app).
COHORT_TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND")


def run_cohort_all(spark, client: RiotClient, tiers=None, sample_size: int = 25,
                   matches_per_player: int = 4) -> dict:
    """Seed a cohort for EVERY tier and rebuild the benchmark tables once, so the
    app has real per-tier benchmarks for each level in the Target Tier dropdown.
    """
    tiers = tiers or COHORT_TIERS
    ensure_schema(spark)
    summary = {}
    for tier in tiers:
        summary[tier] = ingest_cohort(
            spark, client, tier=tier, sample_size=sample_size,
            matches_per_player=matches_per_player)
    build_silver(spark)
    build_gold_benchmarks(spark)
    build_challenge_benchmarks(spark)
    return summary
