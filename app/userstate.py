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
]


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
