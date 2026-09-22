"""Dash entry point for the League AI Coach Databricks App.

Binds 0.0.0.0:$DATABRICKS_APP_PORT (required by the Apps platform — binding to
localhost or a hardcoded port causes 502s).
"""

import os
import sys

# Make the `src` source root importable.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import dash  # noqa: E402

from callbacks import register_callbacks  # noqa: E402
from layout import serve_layout  # noqa: E402


def _egress_check():
    """Log whether this app process can reach the public internet (Riot).
    Determines whether we can fetch matches synchronously in-app."""
    import urllib.request
    try:
        with urllib.request.urlopen(
            "https://ddragon.leagueoflegends.com/api/versions.json", timeout=8
        ) as r:
            print(f"[EGRESS-CHECK] external internet reachable (status {r.status})",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[EGRESS-CHECK] external internet NOT reachable: {exc!r}", flush=True)


_egress_check()


def _apply_saved_destination():
    """Repoint UC to the catalog/schema chosen in the first-run wizard (stored in
    the _app_config pointer) before serving, so reads/writes/job params follow it.
    Best-effort: on a cold warehouse or first run this no-ops and the wizard shows."""
    try:
        import data_access

        cfg = data_access.apply_active_destination()
        if cfg:
            print(f"[SETUP] active destination: {cfg.get('catalog')}."
                  f"{cfg.get('schema')} (setup_complete={cfg.get('setup_complete')})",
                  flush=True)
        else:
            print("[SETUP] no saved destination yet — first-run wizard will show.",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[SETUP] could not read saved destination: {exc!r}", flush=True)


_apply_saved_destination()

app = dash.Dash(__name__, title="Abyssal Insight", update_title=None)
app.layout = serve_layout
register_callbacks(app)

# Gunicorn/WSGI entry point if ever served that way.
server = app.server

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("DATABRICKS_APP_PORT", 8000)),
        debug=False,
    )
