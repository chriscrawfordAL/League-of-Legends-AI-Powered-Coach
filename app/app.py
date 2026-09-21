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
