"""Per-user application state on Lakebase (Lakebase use case #2).

Stores each app user's saved players (watchlist), UI preferences, and recent
searches in app-owned Postgres tables in the Lakebase instance. The app's
service-principal Postgres role has CAN_CONNECT_AND_CREATE, so the app owns and
bootstraps these tables itself — no extra grants.

Identity comes from the Databricks Apps user header (X-Forwarded-Email); when
absent (local dev / no user auth) everything falls back to a shared 'anonymous'
user. All access degrades gracefully when Lakebase is unavailable (empty lists /
no-op writes), so the app is fully functional without it.
"""

from __future__ import annotations

import threading
import time

import lakebase

_SCHEMA = "app_state"
_bootstrapped = False
_boot_lock = threading.Lock()

_DDL = [
    f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.saved_players (
        user_id text NOT NULL, riot_id text NOT NULL, region text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, riot_id, region))""",
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.user_prefs (
        user_id text PRIMARY KEY, role text, tier text, queue text,
        timeframe int, sample_count int,
        updated_at timestamptz NOT NULL DEFAULT now())""",
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.search_history (
        id bigserial PRIMARY KEY, user_id text NOT NULL, riot_id text NOT NULL,
        region text NOT NULL, searched_at timestamptz NOT NULL DEFAULT now())""",
    # App config (the first-run wizard's active-destination pointer, moved off
    # the Delta/warehouse table into Postgres) + ingestion job-run history.
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.app_config (
        key text PRIMARY KEY, catalog_name text, schema_name text,
        setup_complete boolean, updated_at timestamptz NOT NULL DEFAULT now())""",
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.job_runs (
        run_id bigint PRIMARY KEY, kind text, catalog_name text, schema_name text,
        status text, started_by text,
        started_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    # Riot API usage counter (per fixed time window) — a shared, atomic counter
    # so the app can meter usage against the dev key's rate-limit budget across
    # requests and instances. Plus a durable, shared FMAPI response cache.
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.api_usage (
        window_start bigint PRIMARY KEY, calls int NOT NULL DEFAULT 0)""",
    f"""CREATE TABLE IF NOT EXISTS {_SCHEMA}.llm_cache (
        cache_key text PRIMARY KEY, response text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""",
]

# Riot dev-key budget: ~100 requests / 2 minutes (the binding limit for the app).
RIOT_WINDOW_SECONDS = 120
RIOT_WINDOW_LIMIT = 100


def _ensure() -> bool:
    """Create the app_state schema/tables once per process (idempotent)."""
    global _bootstrapped
    if _bootstrapped or not lakebase.enabled():
        return _bootstrapped
    with _boot_lock:
        if _bootstrapped:
            return True
        _bootstrapped = all(lakebase.execute(ddl) for ddl in _DDL)
        return _bootstrapped


def current_user() -> str:
    """The logged-in app user's email (X-Forwarded-Email), or 'anonymous'."""
    try:
        from flask import request

        for h in ("X-Forwarded-Email", "X-Forwarded-Preferred-Username",
                  "X-Forwarded-User"):
            v = request.headers.get(h)
            if v:
                return v
    except Exception:  # noqa: BLE001 — outside a request context / no header
        pass
    return "anonymous"


# --- Saved players (watchlist) -------------------------------------------
def save_player(riot_id: str, region: str) -> bool:
    if not (riot_id and region) or not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.saved_players (user_id, riot_id, region) "
        "VALUES (%s, %s, %s) ON CONFLICT (user_id, riot_id, region) DO NOTHING",
        (current_user(), riot_id.strip(), region.strip()))


def remove_player(riot_id: str, region: str) -> bool:
    if not _ensure():
        return False
    return lakebase.execute(
        f"DELETE FROM {_SCHEMA}.saved_players "
        "WHERE user_id=%s AND riot_id=%s AND region=%s",
        (current_user(), riot_id, region))


def list_saved_players() -> list[dict]:
    if not _ensure():
        return []
    return lakebase.query(
        f"SELECT riot_id, region FROM {_SCHEMA}.saved_players "
        "WHERE user_id=%s ORDER BY created_at DESC", (current_user(),)) or []


# --- Preferences ---------------------------------------------------------
def get_prefs() -> dict:
    if not _ensure():
        return {}
    rows = lakebase.query(
        f"SELECT role, tier, queue, timeframe, sample_count "
        f"FROM {_SCHEMA}.user_prefs WHERE user_id=%s", (current_user(),)) or []
    return rows[0] if rows else {}


def save_prefs(role=None, tier=None, queue=None, timeframe=None,
               sample_count=None) -> bool:
    if not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.user_prefs "
        "(user_id, role, tier, queue, timeframe, sample_count, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET role=EXCLUDED.role, "
        "tier=EXCLUDED.tier, queue=EXCLUDED.queue, timeframe=EXCLUDED.timeframe, "
        "sample_count=EXCLUDED.sample_count, updated_at=now()",
        (current_user(), role, tier, queue, timeframe, sample_count))


# --- Recent searches -----------------------------------------------------
def add_search(riot_id: str, region: str) -> bool:
    if not (riot_id and region) or not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.search_history (user_id, riot_id, region) "
        "VALUES (%s, %s, %s)", (current_user(), riot_id.strip(), region.strip()))


def recent_searches(limit: int = 5) -> list[dict]:
    if not _ensure():
        return []
    return lakebase.query(
        f"SELECT riot_id, region, MAX(searched_at) AS last_at "
        f"FROM {_SCHEMA}.search_history WHERE user_id=%s "
        "GROUP BY riot_id, region ORDER BY last_at DESC LIMIT %s",
        (current_user(), int(limit))) or []


# --- App config pointer (first-run wizard destination) -------------------
def get_config() -> dict | None:
    """The active-destination pointer {catalog, schema, setup_complete}, or None."""
    if not _ensure():
        return None
    rows = lakebase.query(
        f"SELECT catalog_name AS catalog, schema_name AS schema, setup_complete "
        f"FROM {_SCHEMA}.app_config WHERE key='active'")
    if not rows:
        return None
    r = rows[0]
    return {"catalog": r.get("catalog"), "schema": r.get("schema"),
            "setup_complete": bool(r.get("setup_complete"))}


def set_config(catalog: str, schema: str, setup_complete: bool) -> bool:
    if not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.app_config "
        "(key, catalog_name, schema_name, setup_complete, updated_at) "
        "VALUES ('active', %s, %s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET catalog_name=EXCLUDED.catalog_name, "
        "schema_name=EXCLUDED.schema_name, setup_complete=EXCLUDED.setup_complete, "
        "updated_at=now()",
        (catalog, schema, bool(setup_complete)))


# --- Ingestion job-run history -------------------------------------------
def log_job_run(run_id, kind: str, catalog: str | None = None,
                schema: str | None = None, status: str = "RUNNING") -> bool:
    if not run_id or not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.job_runs "
        "(run_id, kind, catalog_name, schema_name, status, started_by) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (run_id) DO UPDATE SET status=EXCLUDED.status, updated_at=now()",
        (int(run_id), kind, catalog, schema, status, current_user()))


def update_job_run(run_id, status: str) -> bool:
    if not run_id or not _ensure():
        return False
    return lakebase.execute(
        f"UPDATE {_SCHEMA}.job_runs SET status=%s, updated_at=now() WHERE run_id=%s",
        (status, int(run_id)))


def recent_job_runs(limit: int = 5) -> list[dict]:
    if not _ensure():
        return []
    return lakebase.query(
        f"SELECT run_id, kind, status, started_by, started_at "
        f"FROM {_SCHEMA}.job_runs ORDER BY started_at DESC LIMIT %s",
        (int(limit),)) or []


# --- Riot API rate-limit / usage counter ---------------------------------
def _window_start(now: float | None = None) -> int:
    now = now if now is not None else time.time()
    return int(now // RIOT_WINDOW_SECONDS) * RIOT_WINDOW_SECONDS


def record_api_calls(n: int = 1) -> bool:
    """Atomically add ``n`` Riot API calls to the current window's counter."""
    if n <= 0 or not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.api_usage (window_start, calls) VALUES (%s, %s) "
        "ON CONFLICT (window_start) DO UPDATE SET "
        f"calls = {_SCHEMA}.api_usage.calls + EXCLUDED.calls",
        (_window_start(), int(n)))


def api_usage_now() -> dict:
    """Current-window Riot usage: {used, limit, remaining, resets_in, window_seconds}."""
    now = time.time()
    ws = _window_start(now)
    used = 0
    if _ensure():
        rows = lakebase.query(
            f"SELECT calls FROM {_SCHEMA}.api_usage WHERE window_start=%s", (ws,))
        if rows:
            used = int(rows[0].get("calls") or 0)
    return {"used": used, "limit": RIOT_WINDOW_LIMIT,
            "remaining": max(0, RIOT_WINDOW_LIMIT - used),
            "resets_in": int(ws + RIOT_WINDOW_SECONDS - now),
            "window_seconds": RIOT_WINDOW_SECONDS}


# --- Durable, shared FMAPI response cache --------------------------------
def llm_cache_get(key: str) -> str | None:
    if not _ensure():
        return None
    rows = lakebase.query(
        f"SELECT response FROM {_SCHEMA}.llm_cache WHERE cache_key=%s", (key,))
    return rows[0].get("response") if rows else None


def llm_cache_put(key: str, value: str) -> bool:
    if value is None or not _ensure():
        return False
    return lakebase.execute(
        f"INSERT INTO {_SCHEMA}.llm_cache (cache_key, response) VALUES (%s, %s) "
        "ON CONFLICT (cache_key) DO UPDATE SET response=EXCLUDED.response, "
        "created_at=now()", (key, value))
