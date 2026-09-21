"""Dash callbacks — Abyssal Insight. Data -> metrics -> coaching narrative."""

from __future__ import annotations

import datetime
import time
from urllib.parse import parse_qs, quote

from dash import Input, Output, State, ctx, dcc, html, no_update

import config
import data_access
from analysis import coach, metrics


def _start_time_from_days(days):
    if not days:
        return None
    return int(time.time()) - int(days) * 86400


def _filter_cached(rows, start_time, queue_mode):
    """Apply the Configure timeframe + queue filters to cached rows.

    The cache holds whatever was last pulled for a player; narrowing the
    timeframe or queue on a later request must still filter it (otherwise "last
    7 days" would show months-old cached games). ``start_time`` is epoch seconds;
    ``game_creation`` is epoch ms.
    """
    out = rows
    if start_time:
        cutoff_ms = int(start_time) * 1000
        out = [r for r in out if (r.get("game_creation") or 0) >= cutoff_ms]
    qm = (queue_mode or "both").lower()
    if qm == "ranked":
        out = [r for r in out if r.get("queue_category") == "ranked"]
    elif qm == "unranked":
        out = [r for r in out if r.get("queue_category") == "unranked"]
    else:  # both — ranked + unranked practice, excluding ARAM/bot ("other")
        out = [r for r in out if r.get("queue_category") in ("ranked", "unranked")]
    return out


def _item_href(s, r, days, q, m=None, view="itemization"):
    """URL for the itemization/macro page that carries the timeframe/queue context
    so the page honors the same window the Analysis view used."""
    href = (f"?view={view}&s={quote(s)}&r={quote(r)}"
            f"&days={quote(str(days if days is not None else 0))}&q={quote(q or 'both')}")
    if m:
        href += f"&m={quote(m)}"
    return href


def _parse_item_filters(qs):
    """Read (days:int, queue:str) from a parsed itemization query string."""
    try:
        days = int((qs.get("days") or ["0"])[0] or 0)
    except ValueError:
        days = 0
    return days, (qs.get("q") or ["both"])[0] or "both"


def _itemization_rows(store, s, r, days, q):
    """Games to show on the itemization page.

    Prefer the live ``player-store`` rows — the exact games the Analysis view is
    displaying (already filtered to the window) — so the two views never disagree
    when Analysis is showing freshly-fetched games not yet/again in the table.
    Fall back to the filtered persisted table for direct URL navigation (where
    player-store is empty), so a pasted/bookmarked link still works.
    """
    store = store or {}
    if store.get("rows") and store.get("label") == f"{s}#{r}":
        return list(store["rows"])
    return _filter_cached(data_access.load_summoner_table(s, r),
                          _start_time_from_days(days), q)


def _macro_context(qs, store):
    """Resolve (s, r, days, q) for the macro page from the URL, falling back to the
    loaded player (player-store) so a static '?view=macro' link still shows the
    player's recent games."""
    s, r = (qs.get("s") or [""])[0], (qs.get("r") or [""])[0]
    days, q = _parse_item_filters(qs)
    store = store or {}
    if not s and store.get("rows") and "#" in (store.get("label") or ""):
        s, _, r = store["label"].partition("#")
        if "days" not in qs:
            days = store.get("days", 0)
        if "q" not in qs:
            q = store.get("queue", "both")
    return s, r, days, q


def register_callbacks(app) -> None:
    # Light/dark toggle — runs in the browser, flips the `light` class on <body>
    # (CSS + the rift background swap key off that class). Icon swap is pure CSS.
    app.clientside_callback(
        "function(n){ if(n){ document.body.classList.toggle('light'); } "
        "return document.body.classList.contains('light') ? 'light' : 'dark'; }",
        Output("theme-store", "data"),
        Input("theme-toggle", "n_clicks"),
        prevent_initial_call=True,
    )

    # Working overlay: show it the instant Initiate is clicked (clientside → instant
    # feedback during the ~10-13s server fetch; the backdrop blocks other clicks).
    app.clientside_callback(
        "function(n){ return n ? {'display':'flex'} : window.dash_clientside.no_update; }",
        Output("working-overlay", "style"),
        Input("refresh-btn", "n_clicks"),
        prevent_initial_call=True,
    )

    # Cancel: record which request was cancelled (the current Initiate click count),
    # hide the overlay, and return to Configure. The in-flight server fetch can't be
    # aborted mid-request, but its result is discarded by the nav gate below.
    app.clientside_callback(
        "function(n, refreshN){ var nu=window.dash_clientside.no_update; "
        "if(!n){ return [nu,nu,nu]; } "
        "return [refreshN||0, {'display':'none'}, 'configure']; }",
        Output("cancelled", "data"),
        Output("working-overlay", "style", allow_duplicate=True),
        Output("view-tabs", "value", allow_duplicate=True),
        Input("cancel-btn", "n_clicks"),
        State("refresh-btn", "n_clicks"),
        prevent_initial_call=True,
    )

    # When _initiate finishes it writes nav-store: switch to Analysis but KEEP the
    # overlay up (the analysis still has to render). On error (no target) or a
    # cancelled request, hide the overlay instead (nothing will render).
    app.clientside_callback(
        "function(nav, cancelled){ var hide={'display':'none'}, nu=window.dash_clientside.no_update; "
        "if(!nav){ return [nu,nu]; } "
        "if(!nav.target){ return [hide,nu]; } "
        "if(typeof nav.seq==='number' && cancelled===nav.seq){ return [hide,nu]; } "
        "return [nu, nav.target]; }",
        Output("working-overlay", "style", allow_duplicate=True),
        Output("view-tabs", "value", allow_duplicate=True),
        Input("nav-store", "data"),
        State("cancelled", "data"),
        prevent_initial_call=True,
    )

    # The whole analysis renders in one shot (_render). Once its output lands, hide
    # the overlay — revealing the fully-populated page instead of loading bars.
    app.clientside_callback(
        "function(_children){ return {'display':'none'}; }",
        Output("working-overlay", "style", allow_duplicate=True),
        Input("coach-feedback", "children"),
        prevent_initial_call=True,
    )

    # Initiate Sequence (button) OR reload-with-summoner (?summoner=&region= in
    # the URL). The button fetches a fast preview live; long windows also kick
    # off a background backfill. The URL path loads a player's full per-player
    # table after the "Reload for full analysis" button.
    @app.callback(
        Output("refresh-status", "children"),
        Output("nav-store", "data"),  # {seq, target}: clientside hides overlay + navigates
        Output("player-store", "data"),
        Output("backfill-store", "data"),
        Output("backfill-poll", "disabled"),
        Output("backfill-banner", "children"),
        Input("refresh-btn", "n_clicks"),
        Input("url", "search"),
        State("riot-id-input", "value"),
        State("region-input", "value"),
        State("queue-dropdown", "value"),
        State("count-input", "value"),
        State("timeframe-dropdown", "value"),
        State("force-refresh", "value"),
        prevent_initial_call=False,  # also fires on load to handle ?summoner reload
    )
    def _initiate(n_clicks, search, game_name, region_code, queue_mode, count,
                  timeframe_days, force_refresh):
        # --- Reload path: page loaded with ?summoner=&region= -> read the table ---
        if ctx.triggered_id in (None, "url"):
            qs = parse_qs((search or "").lstrip("?"))
            sm, rg = (qs.get("summoner") or [""])[0], (qs.get("region") or [""])[0]
            if sm and rg:
                rows = data_access.load_summoner_table(sm, rg)
                if rows:
                    label = f"{sm}#{rg}"
                    return (f"Loaded full history ({len(rows)} games) for {label}.",
                            {"seq": "reload", "target": "analysis"},
                            {"rows": rows, "label": label, "error": None,
                             "days": 0, "queue": "both"},
                            {}, True, [])
            return (no_update,) * 6  # plain load, nothing to do

        # --- Initiate Sequence (button) ---
        gname = (game_name or "").strip()
        region_code = (region_code or "").strip().upper()
        if not gname or not region_code:
            return ("Enter both a Riot ID and a Region (e.g. Faker / NA1).",
                    {"seq": n_clicks, "target": None},  # done, no nav (hides overlay)
                    no_update, no_update, no_update, no_update)

        platform, region = config.routing_for(region_code)
        qmode, cnt = queue_mode or "both", int(count or config.MATCH_FETCH_COUNT)
        full_start = _start_time_from_days(timeframe_days)
        rid = f"{gname}#{region_code}"
        force = "force" in (force_refresh or [])

        # Cache: unless "Refresh from Riot" is checked, read this player's saved
        # table first — instant, and no Riot call (dodges the rate limit). The
        # cache is filtered to the chosen timeframe/queue; if nothing matches the
        # window we fall through to a live pull (the cache may just be stale).
        if not force:
            cached = _filter_cached(
                data_access.load_summoner_table(gname, region_code), full_start, qmode)
            if cached:
                return (f"Loaded {len(cached)} saved games for {rid} (cached, filtered to "
                        f"your queue/timeframe). Check “Refresh from Riot” to re-pull.",
                        {"seq": n_clicks, "target": "analysis"},
                        {"rows": cached, "label": rid, "error": None,
                         "days": timeframe_days, "queue": qmode},
                        {}, True, [])

        # Cache miss / forced refresh: fetch live (most-recent up-to-MAX_SYNC games
        # in the window). fetch_player_live caps at MAX_SYNC for speed; a 404 /
        # rate-limit / truly-empty window comes back as `error`.
        cap = data_access.MAX_SYNC_MATCHES
        rows, error, _note = data_access.fetch_player_live(
            gname, region_code, platform, region, min(cnt, cap), full_start, qmode)
        if error:
            return (error, {"seq": n_clicks, "target": None},
                    {"rows": [], "label": rid, "error": error},
                    no_update, no_update, no_update)

        # Background only when the user asked for MORE than one fast pull returns
        # AND the preview filled the cap (so there are likely older games beyond).
        if not (cnt > cap and len(rows) >= cap):
            # Persist this player's data so every user ends up with their own table.
            data_access.write_summoner_rows(gname, region_code, rows)
            return (f"Analyzed {len(rows)} games for {rid} (saved to your table).",
                    {"seq": n_clicks, "target": "analysis"},
                    {"rows": rows, "label": rid, "error": None, "days": timeframe_days, "queue": qmode}, {}, True, [])

        # Kick off the full backfill (up to `cnt`) into the per-player table.
        bf = data_access.trigger_backfill(gname, region_code, platform, region,
                                          cnt, full_start, qmode)
        if bf.get("status") == "started":
            store = {"run_id": bf["run_id"], "summoner": gname,
                     "region": region_code, "status": "running"}
            return (f"Showing your {len(rows)} most-recent games for {rid}; pulling "
                    f"up to {cnt} in the background.",
                    {"seq": n_clicks, "target": "analysis"},
                    {"rows": rows, "label": rid, "error": None, "days": timeframe_days, "queue": qmode},
                    store, False, _banner_loading(rid, len(rows), cnt))
        return (f"Showing your {len(rows)} most-recent games for {rid} "
                f"(background pull unavailable: {bf.get('message', '')}).",
                {"seq": n_clicks, "target": "analysis"},
                {"rows": rows, "label": rid, "error": None, "days": timeframe_days, "queue": qmode}, {}, True, [])

    # Poll the background backfill; reveal the reload button when it's done.
    @app.callback(
        Output("backfill-banner", "children", allow_duplicate=True),
        Output("backfill-poll", "disabled", allow_duplicate=True),
        Output("backfill-store", "data", allow_duplicate=True),
        Input("backfill-poll", "n_intervals"),
        State("backfill-store", "data"),
        prevent_initial_call=True,
    )
    def _poll(_n, store):
        store = store or {}
        if store.get("status") != "running" or not store.get("run_id"):
            return no_update, True, no_update
        st = data_access.job_run_status(store["run_id"])
        life, result = st.get("life"), st.get("result")
        if life == "TERMINATED" and result == "SUCCESS":
            return (_banner_ready(store["summoner"], store["region"]), True,
                    {**store, "status": "done"})
        if life in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED") or \
                result in ("FAILED", "TIMEDOUT", "CANCELED"):
            return _banner_failed(), True, {**store, "status": "failed"}
        return no_update, False, no_update  # still running

    # One atomic render of the whole Trend Analysis view (KPIs, readiness, matches,
    # coaching narrative, AND the detailed-metrics table). Merged into a single
    # callback so everything lands together — the "Working" overlay stays up until
    # this completes, then the finished page is revealed (no half-loaded bars).
    @app.callback(
        Output("kpi-row", "children"),
        Output("readiness-panel", "children"),
        Output("matches-table", "data"),
        Output("matches-table", "columns"),
        Output("coach-feedback", "children"),
        Output("player-line", "children"),
        Output("detailed-metrics", "children"),
        Input("player-store", "data"),
        Input("role-dropdown", "value"),
        Input("tier-dropdown", "value"),
        Input("view-tabs", "value"),
    )
    def _render(store, role, tier, tab):
        if tab != "analysis":
            return (no_update,) * 7
        tier = tier or config.BENCHMARK_TIER
        store = store or {}
        rows = store.get("rows") or []
        if store.get("error"):
            err = html.Div(store["error"], style={"color": "#ff6b6b"})
            return ([err], None, [], [], "", f"⚠ {store.get('label', '')}", err)
        if not rows:
            prompt = "Enter a Riot ID + Region on Configure, then Initiate Sequence."
            p = html.Div(prompt, style={"color": "#85A1AD"})
            return ([p], None, [], [], "", prompt, p)

        benchmarks = metrics.benchmarks_from_rows(data_access.load_benchmarks(), tier)
        evaluation = metrics.evaluate(rows, role=role, benchmarks=benchmarks)
        readiness = metrics.readiness_summary(rows, tier)
        kpis = _kpi_cards(evaluation)
        panel = _readiness_panel(readiness)
        cols, data = _matches_table(rows)
        feedback = coach.generate_feedback(evaluation, tier, readiness=readiness)

        # Detailed-metrics table (per-metric AI analysis).
        ch_bench = data_access.load_challenge_benchmarks(tier)
        comparisons = metrics.detailed_comparison(rows, role, ch_bench)
        if not any(c["gold"] is not None for c in comparisons):
            detailed = html.Div(
                f"No {tier} benchmark data for the {role} role yet — pick a different "
                f"Target Tier or role that has data.", style={"color": "#e6c01a"})
        else:
            analysis = coach.analyze_metrics(comparisons, tier)
            detailed = _detailed_table(comparisons, analysis, role, len(rows), tier)

        return (kpis, panel, data, cols, feedback,
                f"Analyzing {store.get('label', '')}", detailed)

    # The "Itemization Analysis" entry button: visible only on the Analysis view
    # once a player is loaded, with its href carrying that player.
    @app.callback(
        Output("itemization-link", "href"),
        Output("itemization-link", "style"),
        Input("player-store", "data"),
        Input("view-tabs", "value"),
    )
    def _itemization_link(store, tab):
        store = store or {}
        label = store.get("label") or ""
        if tab != "analysis":
            return no_update, no_update  # button isn't in the DOM off this tab
        if not store.get("rows") or "#" not in label:
            return "#", {"display": "none"}
        name, _, region = label.partition("#")
        # Carry the timeframe/queue actually used (recorded in the store by
        # _initiate) so the itemization page filters to the same window.
        href = _item_href(name, region, store.get("days", 0), store.get("queue", "both"))
        return href, {"display": "inline-flex", "alignItems": "center",
                      "textDecoration": "none"}

    # Single owner of page visibility: main vs itemization vs macro, from the URL.
    @app.callback(
        Output("main-view", "style"),
        Output("itemization-view", "style"),
        Output("macro-view", "style"),
        Input("url", "search"),
    )
    def _route(search):
        view = (parse_qs((search or "").lstrip("?")).get("view") or [""])[0]
        hide = {"display": "none"}
        if view == "itemization":
            return hide, {}, hide
        if view == "macro":
            return hide, hide, {}
        return {}, hide, hide

    # Navigate to a pasted game ID (clientside → no reload, player-store preserved).
    app.clientside_callback(
        "function(n, gid, store){ var nu=window.dash_clientside.no_update; "
        "if(!n || !gid || !gid.trim()){ return nu; } "
        "var q='?view=macro&m='+encodeURIComponent(gid.trim()); "
        "if(store && store.label && store.label.indexOf('#')>-1){ var p=store.label.split('#'); "
        "q+='&s='+encodeURIComponent(p[0])+'&r='+encodeURIComponent(p[1]); } "
        "return q; }",
        Output("url", "search"),
        Input("macro-analyze-btn", "n_clicks"),
        State("macro-gameid-input", "value"),
        State("player-store", "data"),
        prevent_initial_call=True,
    )

    # Itemization PAGE content: ?view=itemization&s=<name>&r=<region>[&m=<matchId>]
    @app.callback(
        Output("item-header", "children"),
        Output("item-game-list", "children"),
        Output("item-back", "href"),
        Input("url", "search"),
        State("player-store", "data"),
    )
    def _item_view(search, store):
        qs = parse_qs((search or "").lstrip("?"))
        if (qs.get("view") or [""])[0] != "itemization":
            return no_update, no_update, no_update
        s, r = (qs.get("s") or [""])[0], (qs.get("r") or [""])[0]
        m = (qs.get("m") or [""])[0]
        days, q = _parse_item_filters(qs)
        # Use the games Analysis is showing (player-store); fall back to the table.
        rows = _itemization_rows(store, s, r, days, q)[:10]
        header = f"Itemization — {s}#{r}" + (f" · last {days} days" if days else "")
        return (header, _item_game_list(rows, s, r, days, q, m),
                f"?summoner={quote(s)}&region={quote(r)}")

    # Macro PAGE: recent-games picker + back link. ?view=macro[&s&r&days&q][&m]
    @app.callback(
        Output("macro-recent-list", "children"),
        Output("macro-back", "href"),
        Input("url", "search"),
        State("player-store", "data"),
    )
    def _macro_view(search, store):
        qs = parse_qs((search or "").lstrip("?"))
        if (qs.get("view") or [""])[0] != "macro":
            return no_update, no_update
        m = (qs.get("m") or [""])[0]
        s, r, days, q = _macro_context(qs, store)
        back = f"?summoner={quote(s)}&region={quote(r)}" if s and r else "?"
        if not s:
            return (html.Div("No player loaded — paste a game ID above to review any game.",
                             style={"color": "#85A1AD"}), back)
        rows = _itemization_rows(store, s, r, days, q)[:10]
        return _item_game_list(rows, s, r, days, q, m, view="macro"), back

    # Macro analysis for the selected/pasted game (Riot match + timeline + LLM).
    @app.callback(
        Output("macro-analysis", "children"),
        Input("url", "search"),
        State("player-store", "data"),
    )
    def _macro_analysis(search, store):
        qs = parse_qs((search or "").lstrip("?"))
        if (qs.get("view") or [""])[0] != "macro":
            return no_update
        m = (qs.get("m") or [""])[0]
        if not m:
            return html.Div("Paste a game ID and click Analyze, or pick a recent game above.",
                            className="ab-panel", style={"color": "#85A1AD"})
        s, r, days, q = _macro_context(qs, store)
        # Explicit team focus from a clicked team card overrides the puuid focus.
        fp = (qs.get("focus") or [""])[0]
        focus_team = int(fp) if fp in ("100", "200") else None
        # For the user's own games we know their puuid (focus their team); a pasted
        # arbitrary id won't match -> None -> neutral both-teams review.
        src = _itemization_rows(store, s, r, days, q) if s else []
        focus_puuid = next((row.get("puuid") for row in src if row.get("match_id") == m), None)
        facts, error = data_access.fetch_macro(r, m, focus_puuid, focus_team)
        if error:
            return html.Div(error, className="ab-panel", style={"color": "#ff6b6b"})
        narrative = coach.analyze_macro(facts)
        return _macro_panel(facts, narrative, s, r, days, q, m)

    # Itemization analysis for the selected game (Riot match + timeline + LLM).
    @app.callback(
        Output("item-analysis", "children"),
        Input("url", "search"),
        State("player-store", "data"),
    )
    def _item_analysis(search, store):
        qs = parse_qs((search or "").lstrip("?"))
        if (qs.get("view") or [""])[0] != "itemization":
            return no_update
        s, r = (qs.get("s") or [""])[0], (qs.get("r") or [""])[0]
        m = (qs.get("m") or [""])[0]
        days, q = _parse_item_filters(qs)
        src_rows = _itemization_rows(store, s, r, days, q)
        if not m:
            # No game selected: itemization trends across the last few games (#2).
            trends, per_game, note = data_access.fetch_item_trends(s, r, src_rows, cap=6)
            if not trends.get("games"):
                return html.Div("Select a game above to analyze its itemization "
                                "(no games available for trend analysis).",
                                className="ab-panel", style={"color": "#85A1AD"})
            narrative = coach.analyze_item_trends(trends, per_game)
            return _trends_panel(trends, narrative, note)
        # A game is selected: per-game build + timeline analysis (#3).
        puuid = next((row.get("puuid") for row in src_rows
                      if row.get("match_id") == m), None)
        facts, error = data_access.fetch_itemization(s, r, m, puuid)
        if error:
            return html.Div(error, className="ab-panel", style={"color": "#ff6b6b"})
        verdict = coach.analyze_itemization(facts)
        return _item_analysis_panel(facts, verdict)

# --------------------------------------------------------------------------
# Render helpers (Abyssal Insight styling)
# --------------------------------------------------------------------------
def _banner_loading(rid, shown, total):
    return html.Div(className="backfill-banner loading", children=[
        html.Span("⏳ ", style={"marginRight": "6px"}),
        html.Span(f"Showing your {shown} most-recent games. Pulling up to {total} "
                  f"for {rid} in the background — this can take several minutes "
                  f"(dev-key rate limit). A button will appear here when it's ready."),
    ])


def _banner_ready(summoner, region):
    return html.Div(className="backfill-banner ready", children=[
        html.Span("✓ Your full history is ready. ",
                  style={"marginRight": "10px", "fontWeight": "700"}),
        html.A("Reload for full analysis",
               href=f"?summoner={quote(summoner)}&region={quote(region)}",
               className="ab-btn",
               style={"textDecoration": "none", "fontSize": "13px", "padding": "8px 18px"}),
    ])


def _banner_failed():
    return html.Div(
        "Background pull didn’t complete — showing your last 30 days. Try again later.",
        className="backfill-banner failed")


def _kpi_cards(evaluation):
    player = evaluation.get("player", {})
    if not player:
        return [html.Div("No matches loaded yet — Initiate Sequence on the Configure tab.",
                         style={"color": "#85A1AD"})]
    cards = [
        ("Games analyzed", f"{player.get('games', 0)}"),
        ("Win rate", f"{player.get('winrate', 0):.0%}"),
        ("KDA", f"{player.get('kda', 0):.2f}"),
        ("CS / min", f"{player.get('cs_per_min', 0):.1f}"),
        ("Vision / min", f"{player.get('vision_per_min', 0):.2f}"),
    ]
    return [
        html.Div(className="ab-card", style={"flex": "1", "minWidth": "150px"}, children=[
            html.Div(label, className="label"), html.Div(value, className="value")])
        for label, value in cards
    ]


def _readiness_panel(readiness):
    total = readiness.get("total_games", 0)
    if not total:
        return None
    tier = readiness.get("target_tier", "GOLD")
    enough = readiness.get("practicing_enough", False)
    color = "#1ae6a3" if enough else "#e6c01a"
    msg = (f"Practicing enough to climb toward {tier}." if enough
           else f"Play more UNRANKED games to drill fundamentals for {tier}.")
    return html.Div(className="readiness", children=[
        html.Span("Ranked readiness:", style={"fontFamily": "Cinzel, serif", "color": "var(--teal)"}),
        html.Span(f"{readiness.get('ranked_games', 0)} ranked · "
                  f"{readiness.get('unranked_games', 0)} unranked (practice) · "
                  f"{readiness.get('practice_ratio', 0):.0%} practice — "),
        html.Span(msg, style={"color": color, "fontWeight": "700"}),
    ])


def _fmt_val(v, fmt):
    if v is None:
        return "—"
    return f"{v:.0%}" if fmt == "pct" else f"{v:.1f}"


_VERDICT_COLOR = {"below": "#ff6b6b", "above": "#1ae6a3", "on_par": "#85A1AD", "no_data": "#5b6b70"}


def _detailed_table(comparisons, analysis, role, n_games, tier):
    head = html.Tr([
        html.Th("Metric"), html.Th("Your avg", className="num"),
        html.Th(f"{tier} avg", className="num"), html.Th("Δ", className="num"),
        html.Th("AI analysis"),
    ])
    rows = []
    for c in comparisons:
        color = _VERDICT_COLOR.get(c["verdict"], "#85A1AD")
        delta = "—" if c["delta_pct"] is None else f"{c['delta_pct']:+.0%}"
        rows.append(html.Tr([
            html.Td(c["label"], className="metric-name"),
            html.Td(_fmt_val(c["player"], c["fmt"]), className="num"),
            html.Td(_fmt_val(c["gold"], c["fmt"]), className="num"),
            html.Td(delta, className="num", style={"color": color, "fontWeight": "700"}),
            html.Td(analysis.get(c["key"], ""), style={"color": "var(--text)"}),
        ]))
    return html.Div([
        html.Div(f"Analyzing {n_games} games as {role} vs {tier}. "
                 f"Δ vs the {tier} average (deaths inverted, so red = worse).",
                 style={"color": "#85A1AD", "fontSize": "13px", "marginBottom": "10px"}),
        html.Table(className="metric-table", children=[
            html.Thead(head), html.Tbody(rows)]),
    ])


def _played_at(game_creation):
    """Format Riot gameCreation (epoch ms) as a readable UTC date/time."""
    try:
        return datetime.datetime.utcfromtimestamp(int(game_creation) / 1000).strftime(
            "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _item_game_list(rows, s, r, days, q, selected, view="itemization"):
    """Last-10 games picker (within the selected window); each match id links to
    that game's analysis (itemization or macro), preserving the filter."""
    if not rows:
        return html.Div("No saved games in this window — widen the timeframe on "
                        "Configure, or load games on the Analysis tab first.",
                        style={"color": "#85A1AD"})
    head = html.Tr([html.Th("Game ID"), html.Th("Played (UTC)"), html.Th("Champion"),
                    html.Th("Pos"), html.Th("Mode"), html.Th("Result")])
    trs = []
    for m in rows:
        mid = m.get("match_id") or ""
        is_sel = mid == selected
        href = _item_href(s, r, days, q, mid, view=view)
        win = m.get("win")
        trs.append(html.Tr(className="item-game-row" + (" sel" if is_sel else ""), children=[
            html.Td(dcc.Link(mid or "—", href=href, className="item-game-link")),
            html.Td(_played_at(m.get("game_creation"))),
            html.Td(m.get("champion") or "—"),
            html.Td(m.get("team_position") or "—"),
            html.Td(config.queue_name(m.get("queue_id"))),
            html.Td("Win" if win else "Loss",
                    style={"color": "#1ae6a3" if win else "#ff6b6b", "fontWeight": "700"}),
        ]))
    return html.Table(className="metric-table item-game-table",
                      children=[html.Thead(head), html.Tbody(trs)])


def _item_chips(decorated):
    """A row of item icon + name chips from [{id,name,icon}]."""
    if not decorated:
        return html.Div("—", style={"color": "#85A1AD"})
    return html.Div(className="item-chips", children=[
        html.Div(className="item-chip", children=[
            html.Img(src=d["icon"], className="item-icon", alt=d["name"], title=d["name"]),
            html.Span(d["name"], className="item-chip-name"),
        ]) for d in decorated
    ])


def _champion_badge(champ, icon, sub):
    return html.Div(className="champ-badge", children=[
        html.Img(src=icon, className="champ-icon", alt=champ or "", title=champ or ""),
        html.Div([html.Div(champ or "Unknown", className="champ-name"),
                  html.Div(sub, className="champ-sub")]),
    ])


def _item_analysis_panel(facts, verdict):
    player = facts.get("player", {})
    opp = facts.get("opponent")
    blocks = [
        html.Div(className="ab-panel", children=[
            _champion_badge(player.get("champion"), player.get("icon"),
                            f"{player.get('role') or 'Unknown lane'} · "
                            f"{'Win' if player.get('win') else 'Loss'} · {facts.get('queue', '')}"),
            html.Div("Starting items", className="item-subhead"),
            _item_chips(facts.get("starting_items_dec")),
            html.Div("Final build", className="item-subhead"),
            _item_chips(facts.get("final_items_dec")),
            html.Div("Purchase order", className="item-subhead"),
            _item_chips(facts.get("purchase_order_dec")),
        ]),
    ]
    if opp:
        blocks.append(html.Div(className="ab-panel", children=[
            _champion_badge(opp.get("champion"), opp.get("icon"),
                            f"Lane opponent · {opp.get('role') or ''}"),
            html.Div("Opponent's build", className="item-subhead"),
            _item_chips(facts.get("opponent_items_dec")),
        ]))
    else:
        blocks.append(html.Div("No lane opponent for this game mode — matchup-based "
                               "counter-build advice isn't available.",
                               className="ab-panel", style={"color": "#85A1AD"}))
    # Timeline analysis (#3): gold-lead curve + key stats + combat timing.
    charts = _timeline_charts(facts)
    if charts is not None:
        blocks.append(html.Div("Game Timeline", className="section-h"))
        blocks.append(charts)
    blocks.append(html.Div("AI Itemization Coaching", className="section-h"))
    blocks.append(html.Div(className="ab-panel coach-md", children=dcc.Markdown(verdict)))
    return html.Div(blocks)


def _stat_chip(label, value, color="var(--text)"):
    return html.Div(className="ab-card", style={"flex": "1", "minWidth": "120px"}, children=[
        html.Div(label, className="label"),
        html.Div(value, className="value", style={"color": color}),
    ])


def _signed(v, suffix=""):
    return "—" if v is None else f"{v:+,}{suffix}"


def _timeline_charts(facts):
    """Gold-lead-over-time chart + key stats + combat timing for the selected game."""
    series = facts.get("timeline_series") or []
    if not series:
        return None
    import plotly.graph_objects as go

    minutes = [r["minute"] for r in series]
    has_diff = any(r.get("gold_diff") is not None for r in series)
    fig = go.Figure()
    if has_diff:
        ydiff = [r.get("gold_diff") for r in series]
        fig.add_trace(go.Scatter(x=minutes, y=ydiff, mode="lines", name="Gold lead",
                                 line=dict(color="#1ae6a3", width=3),
                                 fill="tozeroy", fillcolor="rgba(26,230,206,0.10)"))
        # Mark the player's deaths on the curve (where the lead often swings).
        deaths = (facts.get("combat") or {}).get("deaths") or []
        dmins, dvals = [], []
        for dm in deaths:
            row = min(series, key=lambda r: abs(r["minute"] - dm))
            dmins.append(dm)
            dvals.append(row.get("gold_diff"))
        if dmins:
            fig.add_trace(go.Scatter(x=dmins, y=dvals, mode="markers", name="Your deaths",
                                     marker=dict(color="#ff6b6b", size=10, symbol="x")))
        title = "Gold lead vs lane opponent (positive = ahead)"
        yaxis = "Gold difference"
    else:
        fig.add_trace(go.Scatter(x=minutes, y=[r["player_gold"] for r in series],
                                 mode="lines", name="Total gold",
                                 line=dict(color="#1ae6a3", width=3)))
        title = "Your total gold over time"
        yaxis = "Gold"
    fig.add_hline(y=0, line_dash="dot", line_color="#5b6b70")
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#85A1AD", family="Inter, sans-serif"),
        legend=dict(orientation="h", y=1.12, x=0),
        xaxis=dict(title="Minute", gridcolor="rgba(26,77,71,0.35)", zeroline=False),
        yaxis=dict(title=yaxis, gridcolor="rgba(26,77,71,0.35)", zeroline=False),
    )

    ks = facts.get("key_stats") or {}
    combat = facts.get("combat") or {}
    chips = []
    if "gold_diff_at_10" in ks:
        chips.append(_stat_chip("Gold lead @10", _signed(ks.get("gold_diff_at_10")),
                                "#1ae6a3" if (ks.get("gold_diff_at_10") or 0) >= 0 else "#ff6b6b"))
        chips.append(_stat_chip("CS lead @10", _signed(ks.get("cs_diff_at_10")),
                                "#1ae6a3" if (ks.get("cs_diff_at_10") or 0) >= 0 else "#ff6b6b"))
    if "gold_diff_at_15" in ks:
        chips.append(_stat_chip("Gold lead @15", _signed(ks.get("gold_diff_at_15")),
                                "#1ae6a3" if (ks.get("gold_diff_at_15") or 0) >= 0 else "#ff6b6b"))
    chips.append(_stat_chip("Kills / Deaths / Assists",
                            f"{len(combat.get('kills', []))} / {len(combat.get('deaths', []))} "
                            f"/ {len(combat.get('assists', []))}"))
    return html.Div(className="ab-panel", children=[
        html.Div(title, className="item-subhead"),
        dcc.Graph(figure=fig, config={"displayModeBar": False}),
        html.Div(chips, style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                               "marginTop": "10px"}),
    ])


def _trends_panel(trends, narrative, note):
    """Itemization-habits overview across the last N games (#2)."""
    n = trends.get("games", 0)

    def pct_chip(label, key):
        v = trends.get(key, 0)
        # Defensive coverage / boots / anti-heal: higher is generally better;
        # leftover components: lower is better (flag in amber when common).
        good = key != "component_rate"
        color = "#1ae6a3" if (good and v >= 0.6) or (not good and v <= 0.2) else (
            "#e6c01a" if (good and v >= 0.3) or (not good and v <= 0.5) else "#ff6b6b")
        return _stat_chip(label, f"{v:.0%}", color)

    chips = [
        pct_chip("Boots completed", "boots_rate"),
        pct_chip("Built armor", "armor_rate"),
        pct_chip("Built magic resist", "mr_rate"),
        pct_chip("Bought anti-heal", "antiheal_rate"),
        pct_chip("Left components", "component_rate"),
        _stat_chip("Avg completed items", f"{trends.get('avg_real_items', 0):.1f}"),
    ]
    start, cnt = trends.get("most_common_start", ("—", 0))
    head = (f"Itemization habits across your last {n} game(s). Most common opening "
            f"buy: {start} ({cnt}/{n}).")
    children = [
        html.Div(className="ab-panel", children=[
            html.Div(head, style={"color": "#85A1AD", "fontSize": "13px", "marginBottom": "12px"}),
            html.Div(chips, style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),
        ]),
        html.Div("AI Build-Habit Coaching", className="section-h"),
        html.Div(className="ab-panel coach-md", children=dcc.Markdown(narrative)),
        html.Div("Select a game above to drill into a single game's build and timeline.",
                 className="section-sub", style={"marginTop": "14px"}),
    ]
    if note:
        children.insert(0, html.Div(note, className="backfill-banner failed"))
    return html.Div(children)


def _macro_gold_chart(facts):
    """Team gold-lead (Blue − Red) over time for the macro view."""
    series = (facts.get("gold_xp") or {}).get("series") or []
    if len(series) < 2:
        return None
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[s["minute"] for s in series], y=[s["gold_diff"] for s in series],
        mode="lines", name="Gold lead", line=dict(color="#1ae6a3", width=3),
        fill="tozeroy", fillcolor="rgba(26,230,206,0.10)"))
    fig.add_hline(y=0, line_dash="dot", line_color="#5b6b70")
    fig.update_layout(
        height=300, margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#85A1AD", family="Inter, sans-serif"),
        xaxis=dict(title="Minute", gridcolor="rgba(26,77,71,0.35)", zeroline=False),
        yaxis=dict(title="Gold diff (Blue − Red)", gridcolor="rgba(26,77,71,0.35)", zeroline=False))
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


def _macro_team_card(facts, tid, is_focus, href):
    ov = facts["overview"][tid]
    obj = facts["objectives"][tid]
    vis = facts["vision"][tid]
    champs = ov.get("members", [])
    title = f"{ov.get('side')} — {'WIN' if ov.get('win') else 'LOSS'}"
    obj_bits = [f"{obj['dragons']} drakes" + (" (SOUL)" if obj.get("soul") else ""),
                f"{obj['barons']} baron", f"{obj['heralds']} herald",
                f"{obj['grubs']} grubs", f"{obj['towers']} towers"]
    if obj.get("first_blood"):
        obj_bits.append("first blood")
    footer = ("✓ Coaching this team" if is_focus else "▸ Click to analyze this team")
    # Whole card is a link that re-focuses the analysis on this team.
    return dcc.Link(href=href, className="macro-team-card" + (" focus" if is_focus else ""),
                    children=[
        html.Div(title, className="macro-team-title",
                 style={"color": "#1ae6a3" if ov.get("win") else "#ff6b6b"}),
        html.Div(className="macro-champs", children=[
            html.Img(src=m.get("icon"), className="macro-champ-icon",
                     title=f"{m['champion']} · {m['role']}", alt=m.get("champion") or "")
            for m in champs]),
        html.Div(f"{ov.get('kills')} kills · {ov.get('gold'):,} gold", className="macro-team-sub"),
        html.Div(" · ".join(obj_bits), className="macro-team-sub"),
        html.Div(f"Vision {vis['vision_score']} · {vis['wards_placed']} wards · "
                 f"{vis['wards_killed']} cleared · {vis['control_wards']} control",
                 className="macro-team-sub"),
        html.Div(footer, className="macro-team-cta"
                 + (" focus" if is_focus else "")),
    ])


def _macro_lane_table(facts):
    lanes = facts.get("lanes") or []
    if not lanes:
        return None
    head = html.Tr([html.Th("Lane"), html.Th("Blue"), html.Th("Red"),
                    html.Th("Gold Δ@14", className="num"), html.Th("Winner")])
    rows = []
    for l in lanes:
        gd = l.get("gold_diff")
        color = {"Blue": "#1ae6a3", "Red": "#ff6b6b"}.get(l.get("winner"), "#85A1AD")
        rows.append(html.Tr([
            html.Td(l["role"], className="metric-name"), html.Td(l["blue"]), html.Td(l["red"]),
            html.Td("—" if gd is None else f"{gd:+,}", className="num"),
            html.Td(l.get("winner") or "—", style={"color": color, "fontWeight": "700"}),
        ]))
    return html.Table(className="metric-table", children=[html.Thead(head), html.Tbody(rows)])


def _macro_panel(facts, narrative, s, r, days, q, m):
    focus = facts.get("focus_team")
    tf = (facts.get("teamfights") or {}).get("summary", {})

    def focus_href(tid):
        return _item_href(s, r, days, q, m, view="macro") + f"&focus={tid}"

    # NB: explicit `is not None` — a childless Dash component (e.g. dcc.Graph) is
    # falsy via __len__, so `component or fallback` would wrongly pick the fallback.
    chart = _macro_gold_chart(facts)
    lane_tbl = _macro_lane_table(facts)
    no_tl = html.Div("No timeline available.", style={"color": "#85A1AD"})
    blocks = [
        html.Div("Click a team to focus the coaching on that side.", className="section-sub",
                 style={"marginBottom": "8px"}),
        html.Div(style={"display": "flex", "gap": "14px", "flexWrap": "wrap"}, children=[
            _macro_team_card(facts, 100, focus == 100, focus_href(100)),
            _macro_team_card(facts, 200, focus == 200, focus_href(200)),
        ]),
        html.Div("Team Gold Lead", className="section-h"),
        html.Div(className="ab-panel", children=chart if chart is not None else no_tl),
        html.Div("Lane Outcomes @14", className="section-h"),
        html.Div(className="ab-panel", children=lane_tbl if lane_tbl is not None else no_tl),
        html.Div("Teamfights", className="section-h"),
        html.Div(className="ab-panel", children=html.Div(
            f"{tf.get('total', 0)} teamfights — Blue won {tf.get('blue_won', 0)}, "
            f"Red won {tf.get('red_won', 0)}, {tf.get('aces', 0)} ace(s).",
            style={"color": "var(--text)"})),
        html.Div("AI Macro Coaching", className="section-h"),
        html.Div(className="ab-panel coach-md", children=dcc.Markdown(narrative)),
    ]
    return html.Div(blocks)


def _matches_table(matches):
    # (column id, header label)
    cols = [("played", "Played (UTC)"), ("champion", "Champion"),
            ("team_position", "Position"), ("queue", "Mode"), ("win", "Win"),
            ("kills", "K"), ("deaths", "D"), ("assists", "A"),
            ("cs_per_min", "CS/min"), ("vision_score", "Vision")]
    columns = [{"name": label, "id": cid} for cid, label in cols]
    data = []
    for m in matches:
        data.append({
            "played": _played_at(m.get("game_creation")),
            "champion": m.get("champion"),
            "team_position": m.get("team_position") or "—",
            "queue": config.queue_name(m.get("queue_id")),
            "win": m.get("win"),
            "kills": m.get("kills"), "deaths": m.get("deaths"), "assists": m.get("assists"),
            "cs_per_min": round(m["cs_per_min"], 2) if isinstance(m.get("cs_per_min"), float)
            else m.get("cs_per_min"),
            "vision_score": m.get("vision_score"),
        })
    return columns, data
