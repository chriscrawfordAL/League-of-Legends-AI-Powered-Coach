#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["databricks-sdk==0.102.0"]
# ///
# Vendored from fe-vibe/fe-ai-tools/resources/mcp_proxy.py — a generic Databricks
# OAuth stdio<->HTTP MCP relay. Keep it generic: do NOT add LakeDraw-specific logic
# here. LakeDraw config lives in mcp/mcp.json (--server-url/--host/--email-header).
"""MCP Proxy — stdio-to-HTTP relay for remote MCP servers.

Reads JSON-RPC from stdin, forwards to a remote MCP HTTP endpoint with a
Databricks OAuth bearer token, and writes responses to stdout.

Auth follows a three-tier strategy. Claude Code spawns one proxy per
configured MCP server and they start in parallel, so the design has to
cope with two proxies needing OAuth simultaneously.

- **Tier 1** (silent fast path): `auth_type=databricks-cli` reads tokens
  from the CLI's cache at ~/.databricks/token-cache.json. No port binding,
  no browser. Race-safe.
- **Tier 2** (bootstrap): if the CLI cache is missing or stale, the proxy
  shells out to `databricks auth login --host <host>` to populate it. The
  CLI's OAuth callback listener binds localhost:8020, so this is serialized
  across simultaneous proxies via an fcntl file lock — one proxy bootstraps
  at a time.
- **Tier 3** (last-resort fallback): SDK `auth_type=external-browser`. Same
  port-binding caveat; only reached if Tier 2 truly fails (no CLI on PATH,
  CLI subprocess errors, etc.). Inherits the SDK's persistent TokenCache at
  ~/.config/databricks-sdk-py/oauth/, so subsequent sessions are silent.

Identity headers sent with every request are configurable via --email-header
and --session-header, defaulting to X-Sage-User-Email / X-Sage-Session-Id for
back-compat with the Sage MCP server. Other servers (e.g. FEVM) read different
header names — pass the appropriate flags from .mcp.json.
"""

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from databricks.sdk import WorkspaceClient
except ImportError:
    print(
        "[mcp-proxy] databricks-sdk is required. Install with: pip install databricks-sdk",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from databricks.sdk.errors import DatabricksError
except ImportError:
    # SDK layout changed — fall back to a broad catch. Consequence: when this
    # branch is taken, _try_cli_auth's `except (DatabricksError, ValueError, OSError)`
    # widens to bare `except Exception`, which catches programming errors too.
    # Acceptable trade-off for forward compatibility with future SDK layouts.
    DatabricksError = Exception

log = logging.getLogger("mcp-proxy")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[mcp-proxy] %(levelname)s: %(message)s",
)

PROXY_SESSION_ID = str(uuid.uuid4())

parser = argparse.ArgumentParser(description="MCP stdio-to-HTTP proxy with Databricks OAuth")
parser.add_argument("--server-url", required=True, help="Remote MCP server URL")
parser.add_argument("--host", default="", help="Databricks workspace URL for authentication")
parser.add_argument("--email-header", default="X-Sage-User-Email", help="Header name for caller email")
parser.add_argument("--session-header", default="X-Sage-Session-Id", help="Header name for session ID")
args = parser.parse_args()

MCP_SERVER_URL = args.server_url
DATABRICKS_HOST = args.host
EMAIL_HEADER = args.email_header
SESSION_HEADER = args.session_header


class SessionExpiredError(Exception):
    """Raised when the MCP server returns 404 for an unknown session."""


# Module-level state
mcp_session_id: str | None = None
last_init_params: dict | None = None
_user_email: str | None = None
_ws_client: WorkspaceClient | None = None


@contextmanager
def _oauth_port_lock():
    """Serialize operations that bind localhost:8020.

    The OAuth callback port is hardcoded by both the Databricks CLI's Go
    implementation and `databricks-sdk-py`'s external_browser provider. Two
    simultaneous proxies binding it at once is the original bug. This file
    lock (fcntl.flock at /tmp/databricks-cli-oauth-port.lock) serializes any
    operation that may bind the port — Tier 2's CLI subprocess and Tier 3's
    SDK external-browser flow both wrap themselves in this. Falls open
    (yields without locking, with a warning) if the lock file is unusable.
    """
    lock_path = os.path.join(tempfile.gettempdir(), "databricks-cli-oauth-port.lock")
    try:
        lockf = open(lock_path, "w")
    except OSError as exc:
        log.warning(
            "Could not open OAuth lock at %s (%s) — proceeding without serialization.",
            lock_path, exc,
        )
        yield
        return
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)  # blocks until exclusive lock acquired
        yield
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except OSError:
            pass
        lockf.close()


def _try_cli_auth() -> WorkspaceClient | None:
    """Try `auth_type=databricks-cli` against the CLI cache. Returns client on success, None on failure.

    No port binding — reads ~/.databricks/token-cache.json via the CLI. Race-safe.
    databricks-sdk raises a plain ValueError (not DatabricksError) when the CLI
    cache is missing or its refresh token is invalid — that's a normal failure
    mode, not a programming error, so it's in the caught tuple.
    """
    try:
        client = WorkspaceClient(host=DATABRICKS_HOST, auth_type="databricks-cli")
        client.config.authenticate()
        return client
    except (DatabricksError, ValueError, OSError):
        return None


def _bootstrap_cli_login() -> bool:
    """Run `databricks auth login --host X` to populate the CLI token cache.

    Serialized via _oauth_port_lock so simultaneous proxies don't race for
    localhost:8020. Returns True if the subprocess exited 0 (cache likely
    populated), False otherwise.
    """
    with _oauth_port_lock():
        log.info("Bootstrapping CLI cache via `databricks auth login --host %s`", DATABRICKS_HOST)
        try:
            subprocess.run(
                ["databricks", "auth", "login", "--host", DATABRICKS_HOST],
                check=True,
                timeout=300,
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                stderr=sys.stderr,
                start_new_session=True,
            )
            return True
        except FileNotFoundError:
            log.error("databricks CLI not on PATH during bootstrap.")
            return False
        except subprocess.TimeoutExpired:
            log.error("databricks auth login timed out after 5 minutes.")
            return False
        except subprocess.CalledProcessError as exc:
            log.error("databricks auth login failed (exit %d).", exc.returncode)
            return False


def _build_external_browser_client() -> WorkspaceClient:
    """Build a WorkspaceClient via SDK external-browser auth, serialized.

    The SDK's external_browser provider hardcodes the OAuth callback to
    http://localhost:8020 (databricks-sdk 0.102.0, credentials_provider.py:276,
    no Config override). To prevent simultaneous proxies from racing on the
    bind, we wrap the eager authenticate() call in _oauth_port_lock so the
    port-binding portion of the OAuth flow happens under exclusive lock.

    The SDK's persistent TokenCache at ~/.config/databricks-sdk-py/oauth/
    means subsequent sessions silently refresh and don't actually bind the
    port — but the lock is cheap insurance for the cold-cache case.
    """
    kwargs: dict = {"auth_type": "external-browser"}
    if DATABRICKS_HOST:
        kwargs["host"] = DATABRICKS_HOST
    with _oauth_port_lock():
        client = WorkspaceClient(**kwargs)
        client.config.authenticate()  # eager — drives any port binding while locked
    return client


def _build_client() -> WorkspaceClient:
    """Build a WorkspaceClient through the three-tier auth strategy.

    Tier 1: existing CLI cache (silent, race-safe).
    Tier 2: subprocess `databricks auth login` to populate CLI cache (serialized
            via fcntl lock to avoid localhost:8020 collisions across proxies).
    Tier 3: SDK external-browser (last resort; binds localhost:8020 directly).

    Most healthy sessions hit Tier 1 silently. Tier 2 is used on first-run
    setup or refresh-token rotation. Tier 3 is reserved for environments
    where the CLI is absent or its login flow can't complete.
    """
    if shutil.which("databricks") and DATABRICKS_HOST:
        # Tier 1
        client = _try_cli_auth()
        if client is not None:
            return client

        # Tier 2
        log.info(
            "CLI token cache miss for %s — attempting bootstrap via `databricks auth login`.",
            DATABRICKS_HOST,
        )
        if _bootstrap_cli_login():
            client = _try_cli_auth()
            if client is not None:
                return client
            log.warning(
                "Bootstrap completed but CLI auth still fails for %s — "
                "falling back to SDK external-browser auth.",
                DATABRICKS_HOST,
            )
    elif not shutil.which("databricks"):
        log.info("databricks CLI not on PATH — using SDK external-browser auth.")
    elif not DATABRICKS_HOST:
        # CLI is on PATH but no host configured — Tier 1/2 both require --host.
        # Fall through to Tier 3 with an explicit log so this isn't a silent surprise.
        log.info("No --host configured — using SDK external-browser auth (default-profile resolution).")

    # Tier 3 — also serialized via the same lock as Tier 2 so simultaneous
    # proxies that both end up here don't race on localhost:8020 either.
    return _build_external_browser_client()


def _get_client() -> WorkspaceClient:
    global _ws_client
    if _ws_client is None:
        _ws_client = _build_client()
    return _ws_client


def get_auth_headers() -> dict[str, str]:
    """Get authorization headers from the SDK's configured auth chain."""
    return _get_client().config.authenticate()


def get_user_email() -> str:
    """Get the authenticated user's email, cached after first call."""
    global _user_email
    if _user_email is None:
        try:
            me = _get_client().current_user.me()
            email = (me.user_name or "").strip().lower()
            if not email:
                raise RuntimeError("Authenticated user has no user_name email")
            _user_email = email
        except Exception as exc:
            raise RuntimeError("Unable to resolve authenticated caller email") from exc
    return _user_email


def send_request(body: bytes) -> list[str]:
    """POST a JSON-RPC message to the remote MCP server, return response JSON strings."""
    global mcp_session_id

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        SESSION_HEADER: PROXY_SESSION_ID,
        EMAIL_HEADER: get_user_email(),
    }
    headers.update(get_auth_headers())
    if mcp_session_id:
        headers["Mcp-Session-Id"] = mcp_session_id

    req = Request(MCP_SERVER_URL, data=body, headers=headers, method="POST")

    try:
        resp = urlopen(req, timeout=120)
    except HTTPError as exc:
        if exc.code == 404 and mcp_session_id:
            raise SessionExpiredError() from exc
        error_body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc

    # Capture MCP session ID from response
    session = resp.headers.get("Mcp-Session-Id")
    if session:
        mcp_session_id = session

    if resp.status == 202:
        return []

    content_type = resp.headers.get("Content-Type", "")
    data = resp.read().decode()

    if "text/event-stream" in content_type:
        results = []
        current_data: list[str] = []
        for line in data.split("\n"):
            if line.startswith("data: "):
                current_data.append(line[6:])
            elif line == "" and current_data:
                # Empty line = end of SSE event, join multi-line data fields
                results.append("\n".join(current_data))
                current_data = []
        if current_data:
            results.append("\n".join(current_data))
        return results

    return [data] if data.strip() else []


def reinitialize_session() -> bool:
    """Re-initialize the MCP session after expiry."""
    global mcp_session_id
    mcp_session_id = None
    log.info("Re-initializing MCP session...")

    init_request = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": last_init_params
        or {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-proxy", "version": "1.0"},
        },
        "id": f"reinit-{uuid.uuid4()}",
    }

    try:
        send_request(json.dumps(init_request).encode())
        # Per MCP lifecycle, send initialized notification after InitializeResult
        initialized_notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        send_request(json.dumps(initialized_notification).encode())
        log.info("Session re-initialized (session=%s)", mcp_session_id)
        return True
    except Exception as exc:
        log.error("Failed to re-initialize session: %s", exc)
        return False


def process_message(line: str) -> None:
    """Process a single JSON-RPC message from stdin."""
    global last_init_params

    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        log.warning("Skipping malformed JSON: %.100s", line)
        return

    if request.get("method") == "initialize" and "params" in request:
        last_init_params = request["params"]

    try:
        responses = send_request(line.encode())
    except SessionExpiredError:
        if reinitialize_session():
            try:
                responses = send_request(line.encode())
            except Exception as retry_exc:
                log.error("Retry failed: %s", retry_exc)
                responses = None
                error_msg = str(retry_exc)
        else:
            responses = None
            error_msg = "Session expired and re-initialization failed"
    except Exception as exc:
        log.error("Request failed: %s", exc)
        responses = None
        error_msg = str(exc)

    if responses is not None:
        for resp in responses:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()
    elif "id" in request:
        error_response = {
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": error_msg},
            "id": request["id"],
        }
        sys.stdout.write(json.dumps(error_response) + "\n")
        sys.stdout.flush()


def main() -> None:
    log.info("Starting proxy → %s", MCP_SERVER_URL)
    _get_client()  # eager build — auth happens (and any failure surfaces) at startup
    email = get_user_email()
    log.info("Authenticated as %s (session=%s)", email, PROXY_SESSION_ID)

    for line in sys.stdin:
        line = line.strip()
        if line:
            process_message(line)

    log.info("Stdin closed, exiting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error("Fatal: %s", exc)
        sys.exit(1)
