"""Read gold tables and trigger the ingestion job from the Dash app.

Reads use the SQL warehouse (injected as DATABRICKS_WAREHOUSE_ID). On-demand
refresh calls Jobs API run-now on the ingestion job (INGEST_JOB_ID). Both rely
on the app's service principal having the declared resource permissions.
"""

from __future__ import annotations

import os
import threading
from itertools import zip_longest

import config

# Synchronous in-app fetch ceiling. The Databricks Apps proxy times out a request
# at 120s and a dev Riot key allows ~100 requests / 2 min, so we keep each live
# analysis well under that (~90 matches ≈ a few seconds). A production key lifts
# this; larger histories would need a background job.
MAX_SYNC_MATCHES = 90

# A Databricks SQL connection is NOT safe to share across threads, and Dash runs
# callbacks on separate threads — so we keep one connection PER THREAD and
# reconnect+retry once on any failure (also smooths SQL-warehouse cold starts).
_local = threading.local()


def _get_conn():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    from databricks import sql
    from databricks.sdk.core import Config

    cfg = Config()
    warehouse_id = os.environ["DATABRICKS_WAREHOUSE_ID"]
    _local.conn = sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
    )
    return _local.conn


def _query(sql_text: str) -> list[dict]:
    """Run a query on this thread's connection; reconnect+retry once on failure."""
    last = None
    for attempt in (1, 2):
        try:
            with _get_conn().cursor() as cur:
                cur.execute(sql_text)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:  # reset the (possibly broken) connection, retry
            last = exc
            try:
                if getattr(_local, "conn", None):
                    _local.conn.close()
            except Exception:
                pass
            _local.conn = None
    # Table missing / warehouse unavailable -> let callers fall back to stubs.
    return []


def load_player_matches() -> list[dict]:
    """Return the target player's per-match gold rows, newest first ([] if none)."""
    return _query(
        f"SELECT * FROM {config.GOLD_PLAYER_PERFORMANCE} ORDER BY game_creation DESC"
    )


# Explicit per-player table schema (base columns; the 20 challenge metrics are
# appended as DOUBLE). Keeps types stable so reads/metrics behave consistently.
_SUMMONER_COL_TYPES = {
    "match_id": "STRING", "queue_id": "BIGINT", "queue_category": "STRING",
    "game_creation": "BIGINT", "game_duration_s": "BIGINT", "puuid": "STRING",
    "team_id": "BIGINT", "riot_id_game_name": "STRING", "riot_id_tagline": "STRING",
    "champion": "STRING", "team_position": "STRING", "win": "BOOLEAN",
    "kills": "BIGINT", "deaths": "BIGINT", "assists": "BIGINT", "kda": "DOUBLE",
    "cs": "BIGINT", "cs_per_min": "DOUBLE", "gold_earned": "BIGINT",
    "gold_per_min": "DOUBLE", "vision_score": "BIGINT", "vision_per_min": "DOUBLE",
    "damage_to_champions": "BIGINT", "kill_participation": "DOUBLE",
}


def _sql_lit(v) -> str:
    """Render a Python value as a SQL literal (no parameter binding needed)."""
    import math

    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return "NULL"
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _summoner_columns():
    import config

    base = list(_SUMMONER_COL_TYPES)
    extra = [k for k in config.CHALLENGE_KEYS if k not in _SUMMONER_COL_TYPES]
    coltype = {**_SUMMONER_COL_TYPES, **{k: "DOUBLE" for k in extra}}
    return base + extra, coltype


def write_summoner_rows(game_name: str, region: str, rows: list[dict]) -> bool:
    """Persist a player's fetched rows to their own Delta table (create-or-replace).

    Uses rows already fetched from Riot — no extra API calls. Returns True on
    success. The background job writes this same table for large (>cap) pulls.
    """
    if not rows:
        return False
    cols, coltype = _summoner_columns()
    fq = config.summoner_table(game_name, region)
    ddl = ", ".join(f"`{c}` {coltype[c]}" for c in cols)
    collist = ", ".join(f"`{c}`" for c in cols)
    values = ", ".join(
        "(" + ", ".join(_sql_lit(r.get(c)) for c in cols) + ")" for r in rows)
    for attempt in (1, 2):
        try:
            with _get_conn().cursor() as cur:
                cur.execute(f"CREATE OR REPLACE TABLE {fq} ({ddl}) USING DELTA")
                cur.execute(f"INSERT INTO {fq} ({collist}) VALUES {values}")
            return True
        except Exception:
            try:
                if getattr(_local, "conn", None):
                    _local.conn.close()
            except Exception:
                pass
            _local.conn = None
            if attempt == 2:
                return False
    return False


def load_benchmarks() -> list[dict]:
    """Return all gold_rank_benchmarks rows ([] if the table doesn't exist yet).

    Prefers the Lakebase synced online store (low latency, no warehouse cold
    start); falls back to the SQL warehouse if Lakebase is unavailable.
    """
    import lakebase

    rows = lakebase.query(f"SELECT * FROM {lakebase.RANK_SYNCED}")
    if rows is not None:
        return rows
    return _query(f"SELECT * FROM {config.GOLD_RANK_BENCHMARKS}")


def load_summoner_table(game_name: str, region: str) -> list[dict]:
    """Read a player's persisted per-player table, newest first ([] if absent)."""
    return _query(
        f"SELECT * FROM {config.summoner_table(game_name, region)} "
        f"ORDER BY game_creation DESC"
    )


def load_challenge_benchmarks(tier: str | None = None) -> dict:
    """Return {role: {metric_key: gold_avg}} from gold_challenge_benchmarks.

    Filtered to ``tier`` (defaults to the benchmark tier). Empty dict if the
    table doesn't exist yet.
    """
    tier = tier or config.BENCHMARK_TIER
    import lakebase

    # Prefer the Lakebase synced online store; fall back to the SQL warehouse.
    rows = lakebase.query(
        f"SELECT team_position, metric, gold_avg FROM {lakebase.CHALLENGE_SYNCED} "
        f"WHERE tier = %s", (tier,))
    if rows is None:
        rows = _query(
            f"SELECT team_position, metric, gold_avg FROM "
            f"{config.GOLD_CHALLENGE_BENCHMARKS} WHERE tier = '{tier}'")
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["team_position"], {})[r["metric"]] = r["gold_avg"]
    return out


# ---------------------------------------------------------------------------
# First-run setup: catalog/schema selection, pointer persistence, seeding.
# The app is provisioned per-deploy. A small pointer table (_app_config) kept at
# the BOOTSTRAP destination (the deploy-default UC_CATALOG.UC_SCHEMA from env,
# which always exists) records the catalog/schema the user chose in the wizard
# plus whether the tier/role benchmark seed has completed. The wizard seeds the
# benchmark tables by triggering the ingest job in cohort mode.
# ---------------------------------------------------------------------------

_APP_CONFIG_TABLE = "_app_config"


def _bootstrap_schema() -> tuple[str, str]:
    """The fixed (catalog, schema) holding the _app_config pointer — the deploy
    default from env, created at deploy time so it always exists."""
    return (os.environ.get("UC_CATALOG", config.UC_CATALOG),
            os.environ.get("UC_SCHEMA", config.UC_SCHEMA))


def _bootstrap_config_fqn() -> str:
    cat, sch = _bootstrap_schema()
    return f"`{cat}`.`{sch}`.{_APP_CONFIG_TABLE}"


def list_catalogs() -> list[str]:
    """Catalogs the app's service principal can see (SHOW CATALOGS)."""
    out = []
    for r in _query("SHOW CATALOGS"):
        val = (r.get("catalog") or r.get("catalog_name")
               or next(iter(r.values()), None))
        if val:
            out.append(val)
    return sorted(out)


def list_schemas(catalog: str) -> list[str]:
    """Schemas in a catalog (SHOW SCHEMAS IN <catalog>)."""
    if not catalog:
        return []
    out = []
    for r in _query(f"SHOW SCHEMAS IN `{catalog}`"):
        val = (r.get("databaseName") or r.get("namespace") or r.get("schema_name")
               or r.get("database") or next(iter(r.values()), None))
        if val:
            out.append(val)
    return sorted(out)


def create_schema(catalog: str, schema: str) -> tuple[bool, str]:
    """CREATE SCHEMA IF NOT EXISTS. Returns (ok, message); surfaces a clear
    message when the service principal lacks CREATE SCHEMA on the catalog."""
    import re

    if not catalog or not schema:
        return False, "Pick a catalog and enter a schema name."
    if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
        return False, "Schema name may contain only letters, numbers, and underscores."
    for attempt in (1, 2):
        try:
            with _get_conn().cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`")
            return True, f"Schema {catalog}.{schema} is ready."
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            try:
                if getattr(_local, "conn", None):
                    _local.conn.close()
            except Exception:
                pass
            _local.conn = None
            if attempt == 2:
                up = msg.upper()
                if "PERMISSION" in up or "PRIVILEGE" in up or "DENIED" in up:
                    return False, (f"The app can't create schemas in '{catalog}' "
                                   "(missing CREATE SCHEMA). Pick an existing schema "
                                   "or ask an admin to grant it.")
                return False, f"Could not create schema: {msg}"
    return False, "Could not create schema."


def benchmarks_present(catalog: str | None = None, schema: str | None = None) -> bool:
    """True if the tier/role benchmark table exists and has at least one row."""
    cat = catalog or config.UC_CATALOG
    sch = schema or config.UC_SCHEMA
    return bool(_query(
        f"SELECT 1 FROM `{cat}`.`{sch}`.gold_challenge_benchmarks LIMIT 1"))


def load_app_config() -> dict | None:
    """Read the {catalog, schema, setup_complete} pointer, or None if unset."""
    rows = _query(f"SELECT `catalog`, `schema`, `setup_complete` FROM "
                  f"{_bootstrap_config_fqn()} WHERE `key` = 'active'")
    if not rows:
        return None
    r = rows[0]
    return {"catalog": r.get("catalog"), "schema": r.get("schema"),
            "setup_complete": bool(r.get("setup_complete"))}


def save_app_config(catalog: str, schema: str, setup_complete: bool) -> bool:
    """Persist the active-destination pointer at the bootstrap schema."""
    fq = _bootstrap_config_fqn()
    for attempt in (1, 2):
        try:
            with _get_conn().cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {fq} (`key` STRING, `catalog` STRING, "
                    "`schema` STRING, `setup_complete` BOOLEAN, `updated_at` TIMESTAMP) "
                    "USING DELTA")
                cur.execute(f"DELETE FROM {fq} WHERE `key` = 'active'")
                cur.execute(
                    f"INSERT INTO {fq} (`key`, `catalog`, `schema`, `setup_complete`, "
                    f"`updated_at`) VALUES ('active', {_sql_lit(catalog)}, "
                    f"{_sql_lit(schema)}, {_sql_lit(bool(setup_complete))}, "
                    "current_timestamp())")
            return True
        except Exception:  # noqa: BLE001
            try:
                if getattr(_local, "conn", None):
                    _local.conn.close()
            except Exception:
                pass
            _local.conn = None
            if attempt == 2:
                return False
    return False


def apply_active_destination() -> dict | None:
    """Read the pointer and repoint config to the selected destination (so reads,
    writes, and job params all target it). Returns the pointer dict or None."""
    cfg = load_app_config()
    if cfg and cfg.get("catalog") and cfg.get("schema"):
        config.set_destination(cfg["catalog"], cfg["schema"])
    return cfg


def setup_complete() -> bool:
    """True when the first-run wizard is not needed: a chosen destination is
    marked complete AND its benchmark table is populated. As a migration for
    deployments that predate the wizard (or were seeded out of band), a
    already-seeded bootstrap destination is adopted silently."""
    cfg = apply_active_destination()
    if cfg and cfg.get("setup_complete"):
        return benchmarks_present(cfg["catalog"], cfg["schema"])
    # No pointer yet: if the deploy-default destination is already seeded, adopt
    # it so we don't force setup on an already-provisioned deployment.
    cat, sch = _bootstrap_schema()
    if benchmarks_present(cat, sch):
        save_app_config(cat, sch, True)
        config.set_destination(cat, sch)
        return True
    return False


def trigger_cohort(catalog: str, schema: str) -> dict:
    """Run-now the ingest job in cohort mode to seed the tier/role benchmark
    tables into the chosen destination. Returns {status, run_id}."""
    job_id = os.environ.get("INGEST_JOB_ID", "")
    if not job_id:
        return {"status": "unconfigured", "message": "INGEST_JOB_ID not set."}
    params = ["--mode", "cohort", "--catalog", catalog, "--schema", schema]
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        run = w.jobs.run_now(job_id=int(job_id), python_params=params)
        return {"status": "started", "run_id": run.run_id}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


def save_riot_key(key: str) -> tuple[bool, str]:
    """Write the Riot API key to the league_ai_coach secret scope (the ingest job
    reads it there). Requires the app SP to have WRITE on the scope."""
    key = (key or "").strip()
    if not key:
        return False, "Enter a Riot API key."
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        w.secrets.put_secret(scope="league_ai_coach", key="riot_api_key",
                             string_value=key)
        return True, "Riot API key saved."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not save key: {exc}"


def _page_ids(client, puuid, cap, type_, start_time):
    """Paginate match ids up to ``cap`` (match-v5 caps each request at 100)."""
    ids: list[str] = []
    start = 0
    while len(ids) < cap:
        page = client.get_match_ids(puuid, start=start, count=min(100, cap - len(ids)),
                                    type_=type_, start_time=start_time)
        if not page:
            break
        ids.extend(page)
        if len(page) < 100:
            break
        start += len(page)
    return ids[:cap]


def fetch_player_live(game_name, tag_line, platform, region, count, start_time, queue_mode):
    """Fetch a player's recent matches straight from Riot (no Spark, no job).

    Returns (rows, error, note): ``rows`` are per-match rows for the player (same
    shape as the gold table), ``error`` is a user-facing message or None, ``note``
    is an optional advisory (e.g. capped count). Synchronous — runs in the app
    request, bounded by MAX_SYNC_MATCHES.
    """
    key = os.environ.get("RIOT_API_KEY", "")
    if not key:
        return [], "No Riot API key configured on the app.", None
    from riot.client import RiotAPIError, RiotClient
    from riot.models import participant_rows

    requested = int(count or 25)
    cap = min(requested, MAX_SYNC_MATCHES)
    note = (f"Showing your {cap} most-recent games (live dev-key limit)."
            if requested > cap else None)
    # Fail fast on rate limits so a throttled dev key returns a clear message
    # instead of blocking past the app's 120s proxy timeout.
    client = RiotClient(api_key=key, platform=platform, region=region,
                        timeout=10, max_429_wait=3)
    rate_limit_msg = ("Riot is rate-limiting the dev key (429). Wait ~1 minute and "
                      "try again, or lower Sample Size — dev keys allow only ~100 "
                      "requests / 2 min. A production key removes this limit.")

    try:
        acct = client.get_account_by_riot_id(game_name, tag_line)
    except RiotAPIError as exc:
        if "404" in str(exc):
            return [], f"Riot ID “{game_name}#{tag_line}” not found — check the name and region.", None
        if "429" in str(exc):
            return [], rate_limit_msg, None
        return [], f"Riot API error resolving the account: {exc}", None
    except Exception as exc:  # noqa: BLE001
        return [], f"Could not reach Riot: {exc}", None
    puuid = acct["puuid"]

    # Collect match ids. "both" = ranked + normal interleaved by recency; the
    # single-mode cases use the match-v5 type filter directly.
    try:
        if queue_mode == "both":
            ranked = _page_ids(client, puuid, cap, "ranked", start_time)
            normal = _page_ids(client, puuid, cap, "normal", start_time)
            ids, seen = [], set()
            for a, b in zip_longest(ranked, normal):
                for x in (a, b):
                    if x and x not in seen:
                        seen.add(x)
                        ids.append(x)
            ids = ids[:cap]
        else:
            type_ = {"ranked": "ranked", "unranked": "normal"}.get(queue_mode)
            ids = _page_ids(client, puuid, cap, type_, start_time)
    except RiotAPIError as exc:
        if "429" in str(exc):
            return [], rate_limit_msg, None
        return [], f"Riot API error listing matches: {exc}", None

    rows = []
    rate_limited = False
    for mid in ids:
        try:
            match = client.get_match(mid)
        except RiotAPIError as exc:
            if "429" in str(exc):
                rate_limited = True
                break  # budget exhausted — stop and return what we have
            continue
        for r in participant_rows(match):
            if r["puuid"] == puuid:
                rows.append(r)
                break
    if not rows:
        if rate_limited:
            return [], rate_limit_msg, None
        return [], (f"No matches found for {game_name}#{tag_line} in the selected "
                    f"queue/timeframe."), None
    if rate_limited:
        note = (f"Partial: Riot rate-limited the dev key after {len(rows)} games — "
                f"wait ~1 min for the rest.")
    return rows, None, note


def fetch_itemization(game_name, region_code, match_id, puuid):
    """Fetch one game's full item build for the player + lane opponent.

    Two Riot calls (match detail + timeline), then resolves item ids to names and
    icons via Data Dragon. Returns (facts, error): ``facts`` carries both raw id
    lists and decorated {id,name,icon} lists plus *_named name lists for the LLM
    prompt; ``error`` is a user-facing string or None.
    """
    key = os.environ.get("RIOT_API_KEY", "")
    if not key:
        return None, "No Riot API key configured on the app."
    from riot.client import RiotAPIError, RiotClient
    from analysis import itemization
    import ddragon

    platform, region = config.routing_for(region_code)
    client = RiotClient(api_key=key, platform=platform, region=region,
                        timeout=10, max_429_wait=3)
    rate_msg = ("Riot is rate-limiting the dev key (429). Wait ~1 minute and retry "
                "— dev keys allow only ~100 requests / 2 min.")
    try:
        match = client.get_match(match_id)
    except RiotAPIError as exc:
        if "429" in str(exc):
            return None, rate_msg
        if "404" in str(exc):
            return None, f"Match {match_id} not found (it may have expired)."
        return None, f"Riot API error fetching the match: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not reach Riot: {exc}"

    # If we weren't handed the puuid (e.g. table missing it), resolve it.
    if not puuid:
        try:
            puuid = client.get_account_by_riot_id(game_name, region_code)["puuid"]
        except Exception:  # noqa: BLE001
            return None, "Couldn't resolve the player in this match."

    timeline = None
    try:
        timeline = client.get_match_timeline(match_id)
    except Exception:  # noqa: BLE001 - timeline is best-effort (starting items)
        timeline = None

    facts = itemization.build_facts(match, timeline, puuid)
    if not facts.get("found"):
        return None, "This player wasn't found in that match."

    # Decorate item ids with names + icons for rendering, and *_named for the LLM.
    facts["starting_items_dec"] = ddragon.decorate_items(facts["starting_items"])
    facts["final_items_dec"] = ddragon.decorate_items(facts["player"]["final_items"])
    facts["purchase_order_dec"] = ddragon.decorate_items(facts["purchase_order"])
    facts["starting_items_named"] = [d["name"] for d in facts["starting_items_dec"]]
    facts["final_items_named"] = [d["name"] for d in facts["final_items_dec"]]
    facts["purchase_order_named"] = [d["name"] for d in facts["purchase_order_dec"]]
    facts["player"]["icon"] = ddragon.champion_icon(facts["player"]["champion"])
    if facts.get("opponent"):
        facts["opponent_items_dec"] = ddragon.decorate_items(facts["opponent"]["final_items"])
        facts["opponent_items_named"] = [d["name"] for d in facts["opponent_items_dec"]]
        facts["opponent"]["icon"] = ddragon.champion_icon(facts["opponent"]["champion"])
    facts["queue"] = config.queue_name(match.get("info", {}).get("queueId"))

    # Timeline-derived lane-diff curves + combat timing (#3). Best-effort: only if
    # the timeline came back. Opponent id is absent in no-lane modes.
    if timeline:
        from analysis import timeline as tl

        pid = facts["player"].get("participant_id")
        opp_pid = (facts.get("opponent") or {}).get("participant_id")
        if pid:
            series = tl.lane_diff_series(timeline, pid, opp_pid)
            facts["timeline_series"] = series
            facts["key_stats"] = tl.key_stats(series)
            facts["combat"] = tl.combat_timeline(timeline, pid)
    return facts, None


def fetch_item_trends(game_name, region_code, rows, cap=6):
    """Aggregate itemization habits across the player's last ``cap`` games (#2).

    Calls :func:`fetch_itemization` per game (match + timeline), classifies each
    final build via Data Dragon tags, and aggregates. Returns (trends, per_game,
    note). Resilient to rate limits: stops on a 429 and returns partial trends.
    """
    from analysis import itemization
    import ddragon

    per_game, note = [], None
    for row in (rows or [])[:cap]:
        mid = row.get("match_id")
        if not mid:
            continue
        facts, err = fetch_itemization(game_name, region_code, mid, row.get("puuid"))
        if err:
            if "429" in err:
                note = (f"Partial trends: Riot rate-limited the dev key after "
                        f"{len(per_game)} games.")
                break
            continue  # skip a game that failed for another reason
        summary = itemization.summarize_build(
            facts["player"]["final_items"], ddragon.classify_item)
        per_game.append({
            "champion": facts["player"]["champion"],
            "opponent_champion": (facts.get("opponent") or {}).get("champion"),
            "win": facts["player"]["win"],
            "summary": summary,
            "starting_named": facts.get("starting_items_named") or [],
        })
    return itemization.aggregate_trends(per_game), per_game, note


# In-process cache of computed macro facts keyed by match id (the match + timeline
# don't change). Lets switching the focus team reuse the data — no extra Riot calls
# and no recompute — so only the LLM narrative re-runs. Bounded; lost on restart.
_MACRO_FACTS_CACHE: dict[str, dict] = {}
_MACRO_FACTS_ORDER: list[str] = []
_MACRO_FACTS_MAX = 32


def _focus_team_for_puuid(facts, puuid):
    if not puuid:
        return None
    for tid, team in facts.get("overview", {}).items():
        if any(m.get("puuid") == puuid for m in team.get("members", [])):
            return tid
    return None


def fetch_macro(region_code, match_id, focus_puuid=None, focus_team=None):
    """Fetch one game's MACRO (team/game-level) facts for both teams.

    Routing comes from the match-id prefix (e.g. "NA1_..." -> na1/americas) so a
    coach can paste any region's game id; ``region_code`` is a fallback. Two Riot
    calls (match detail + timeline), then the deterministic macro layer, then Data
    Dragon enrichment: champion icons + a per-team resistance assessment against the
    enemy's biggest AD/AP threats. The computed facts are cached by match id so
    re-focusing on the other team is instant. ``focus_team`` (100/200) overrides the
    puuid-derived focus. Returns (facts, error).
    """
    mid = (match_id or "").strip()
    facts = _MACRO_FACTS_CACHE.get(mid)
    if facts is None:
        key = os.environ.get("RIOT_API_KEY", "")
        if not key:
            return None, "No Riot API key configured on the app."
        from riot.client import RiotAPIError, RiotClient
        from analysis import macro
        import ddragon

        prefix = mid.split("_")[0] if "_" in mid else (region_code or "")
        platform, region = config.routing_for(prefix or region_code or "NA1")
        client = RiotClient(api_key=key, platform=platform, region=region,
                            timeout=10, max_429_wait=3)
        try:
            match = client.get_match(mid)
        except RiotAPIError as exc:
            if "429" in str(exc):
                return None, ("Riot is rate-limiting the dev key (429). Wait ~1 min and retry.")
            if "404" in str(exc):
                return None, f"Game {mid} not found — check the ID (it may have expired)."
            return None, f"Riot API error fetching the match: {exc}"
        except Exception as exc:  # noqa: BLE001
            return None, f"Could not reach Riot: {exc}"
        try:
            timeline = client.get_match_timeline(mid)
        except Exception:  # noqa: BLE001 - timeline best-effort (curves/positions degrade)
            timeline = None

        facts = macro.build_macro_facts(match, timeline)

        # Champion icons for the team panels.
        for tid, team in facts.get("overview", {}).items():
            for m in team.get("members", []):
                m["icon"] = ddragon.champion_icon(m.get("champion"))

        # Resistance assessment: against the ENEMY team's biggest AD/AP threats, did
        # this team build armor / magic resist? (the "MR when they needed Armor" check)
        dmg = facts.get("damage", {})
        itemz = {}
        for tid in (macro.BLUE, macro.RED):
            enemy = macro.RED if tid == macro.BLUE else macro.BLUE
            ad = (dmg.get(enemy, {}) or {}).get("biggest_ad")
            ap = (dmg.get(enemy, {}) or {}).get("biggest_ap")
            players = []
            armor_n = mr_n = 0
            for m in facts.get("overview", {}).get(tid, {}).get("members", []):
                flags = [ddragon.classify_item(i) for i in m.get("items", [])]
                has_armor = any(f.get("armor") for f in flags)
                has_mr = any(f.get("mr") for f in flags)
                armor_n += int(has_armor)
                mr_n += int(has_mr)
                players.append({"champion": m["champion"], "role": m["role"],
                                "has_armor": has_armor, "has_mr": has_mr})
            itemz[tid] = {
                "enemy_top_ad": ad["champion"] if ad else None,
                "enemy_top_ap": ap["champion"] if ap else None,
                "players_with_armor": armor_n, "players_with_mr": mr_n,
                "players": players,
            }
        facts["itemization"] = itemz

        _MACRO_FACTS_CACHE[mid] = facts
        _MACRO_FACTS_ORDER.append(mid)
        if len(_MACRO_FACTS_ORDER) > _MACRO_FACTS_MAX:
            _MACRO_FACTS_CACHE.pop(_MACRO_FACTS_ORDER.pop(0), None)

    # Apply the requested focus (explicit team wins; else derive from the puuid).
    facts["focus_team"] = focus_team if focus_team in (100, 200) else \
        _focus_team_for_puuid(facts, focus_puuid)
    return facts, None


def validate_riot_id(game_name: str, tag_line: str, platform: str, region: str):
    """Check a Riot ID exists before kicking off the (slow, async) ingest job.

    Returns True (exists), False (404 not found), or None (couldn't check —
    no key or egress). Uses the app's RIOT_API_KEY secret.
    """
    key = os.environ.get("RIOT_API_KEY", "")
    if not key or not game_name:
        return None
    try:
        from riot.client import RiotAPIError, RiotClient

        client = RiotClient(api_key=key, platform=platform, region=region, timeout=6)
        client.get_account_by_riot_id(game_name, tag_line)
        return True
    except Exception as exc:  # noqa: BLE001
        from riot.client import RiotAPIError

        if isinstance(exc, RiotAPIError) and "404" in str(exc):
            return False
        return None  # network/egress/other -> let the job be the source of truth


def trigger_refresh(
    queue_mode: str = "both",
    count: int = 25,
    start_time: int | None = None,
    game_name: str | None = None,
    tag_line: str | None = None,
    platform: str | None = None,
    region: str | None = None,
    refresh_mode: str = "fresh",
) -> dict:
    """Kick off an on-demand ingestion run via Jobs API run-now.

    The UI toggles are forwarded as python_params to the spark_python_task,
    which argparse-parses them (see jobs/ingest_matches.py).
    """
    job_id = os.environ.get("INGEST_JOB_ID", "")
    if not job_id:
        return {"status": "unconfigured", "message": "INGEST_JOB_ID not set."}
    # run_now python_params REPLACE the task's defaults, so pass the UC
    # destination too. Use the runtime config (repointed by the first-run wizard
    # via apply_active_destination) so job writes follow the chosen destination.
    params = [
        "--catalog", config.UC_CATALOG,
        "--schema", config.UC_SCHEMA,
        "--queue-mode", queue_mode, "--count", str(count),
        "--refresh-mode", refresh_mode,
    ]
    if game_name:
        params += ["--game-name", game_name]
    if tag_line:
        params += ["--tag-line", tag_line]
    if platform:
        params += ["--platform", platform]
    if region:
        params += ["--region", region]
    if start_time is not None:
        params += ["--start-time", str(start_time)]
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        run = w.jobs.run_now(job_id=int(job_id), python_params=params)
        return {"status": "started", "run_id": run.run_id}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def trigger_backfill(game_name, tag_line, platform, region, count, start_time, queue_mode):
    """Run-now the job in summoner mode to backfill the player's full window into
    their per-player table (the background pull). Returns {status, run_id}."""
    job_id = os.environ.get("INGEST_JOB_ID", "")
    if not job_id:
        return {"status": "unconfigured", "message": "INGEST_JOB_ID not set."}
    params = [
        "--mode", "summoner",
        "--catalog", config.UC_CATALOG,
        "--schema", config.UC_SCHEMA,
        "--game-name", game_name, "--tag-line", tag_line,
        "--platform", platform, "--region", region,
        "--queue-mode", queue_mode, "--count", str(count),
    ]
    if start_time is not None:
        params += ["--start-time", str(start_time)]
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        run = w.jobs.run_now(job_id=int(job_id), python_params=params)
        return {"status": "started", "run_id": run.run_id}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


def job_run_status(run_id) -> dict:
    """Return {'life': <lifecycle>, 'result': <result>} for a job run, for
    polling the background backfill. Strings like 'RUNNING' / 'TERMINATED' /
    'SUCCESS'. Empty on error."""
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        run = w.jobs.get_run(run_id=int(run_id))
        state = run.state
        life = str(state.life_cycle_state) if state and state.life_cycle_state else ""
        result = str(state.result_state) if state and state.result_state else ""
        # Enum reprs look like "RunLifeCycleState.TERMINATED"; keep the tail.
        return {"life": life.split(".")[-1], "result": result.split(".")[-1]}
    except Exception as exc:  # noqa: BLE001
        return {"life": "", "result": "", "error": str(exc)}
