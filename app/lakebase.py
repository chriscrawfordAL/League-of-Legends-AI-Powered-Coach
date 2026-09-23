"""Lakebase (Postgres) access — serves the synced gold-benchmark tables.

Lakebase use case #1: the gold benchmark tables are synced from Unity Catalog
into a Lakebase Postgres instance (an online store) and served to the app with
low latency, replacing the SQL-warehouse read path for benchmarks.

Everything degrades gracefully: if Lakebase is unavailable (driver missing, no
instance configured, auth/connect failure) the helpers return ``None`` so
callers fall back to the SQL warehouse — the app never breaks.

Auth: the Databricks Apps ``database`` resource injects PGHOST/PGDATABASE/
PGUSER/PGPORT and a short-lived PGPASSWORD. Because that password is an OAuth
token that rotates (~1h) and env vars are fixed at container start, we mint a
fresh token at connect time via the SDK (falling back to the injected
PGPASSWORD). Connections are thread-local (Dash runs callbacks on threads) with
a reconnect+retry that also re-mints the token.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

# Postgres schema the synced tables materialize into (matches the UC schema).
SYNCED_SCHEMA = "league_ai_coach"
CHALLENGE_SYNCED = f'"{SYNCED_SCHEMA}"."gold_challenge_benchmarks_synced"'
RANK_SYNCED = f'"{SYNCED_SCHEMA}"."gold_rank_benchmarks_synced"'

_local = threading.local()
_token_lock = threading.Lock()
_token = {"value": None, "exp": 0.0}


def _instance() -> str:
    return os.environ.get("LAKEBASE_INSTANCE", "")


def enabled() -> bool:
    """True when Lakebase is wired for this app (instance + host present)."""
    return bool(_instance() and os.environ.get("PGHOST"))


def _password() -> str | None:
    """A valid Postgres password: a freshly minted OAuth token (cached ~45 min,
    well under the ~1h expiry), falling back to the injected PGPASSWORD."""
    now = time.time()
    with _token_lock:
        if _token["value"] and now < _token["exp"]:
            return _token["value"]
    inst = _instance()
    if inst:
        try:
            from databricks.sdk import WorkspaceClient

            cred = WorkspaceClient().database.generate_database_credential(
                request_id=str(uuid.uuid4()), instance_names=[inst])
            tok = getattr(cred, "token", None)
            if tok:
                with _token_lock:
                    _token["value"] = tok
                    _token["exp"] = now + 45 * 60
                return tok
        except Exception:  # noqa: BLE001 — fall back to the injected password
            pass
    return os.environ.get("PGPASSWORD") or None


def _connect():
    import psycopg2

    return psycopg2.connect(
        host=os.environ.get("PGHOST"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "databricks_postgres"),
        user=os.environ.get("PGUSER"),
        password=_password(),
        sslmode="require",
        connect_timeout=10,
    )


def _get_conn():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    _local.conn = _connect()
    return _local.conn


def _reset():
    try:
        if getattr(_local, "conn", None):
            _local.conn.close()
    except Exception:  # noqa: BLE001
        pass
    _local.conn = None
    with _token_lock:  # force a fresh token on reconnect (handles rotation)
        _token["value"] = None
        _token["exp"] = 0.0


def query(sql: str, params: tuple | None = None) -> list[dict] | None:
    """Run a read query; return list[dict], or None if Lakebase is unavailable
    (so callers fall back to the SQL warehouse). Reconnects+retries once."""
    if not enabled():
        return None
    for attempt in (1, 2):
        try:
            with _get_conn().cursor() as cur:
                cur.execute(sql, params)
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:  # noqa: BLE001
            _reset()
            if attempt == 2:
                return None
    return None


def execute(sql: str, params: tuple | None = None) -> bool:
    """Run a write/DDL statement (commit). Returns True on success, False if
    Lakebase is unavailable or the write fails. Reconnects+retries once."""
    if not enabled():
        return False
    for attempt in (1, 2):
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
            return True
        except Exception:  # noqa: BLE001
            _reset()
            if attempt == 2:
                return False
    return False
