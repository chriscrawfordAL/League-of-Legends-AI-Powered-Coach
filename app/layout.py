"""Dash layout — Abyssal Insight theme (ported from the Replit design)."""

from __future__ import annotations

from dash import dash_table, dcc, get_asset_url, html

import config

# Role values the pipeline/benchmarks use, with the design's short labels.
ROLE_OPTIONS = [
    {"label": "TOP", "value": "TOP"},
    {"label": "JGL", "value": "JUNGLE"},
    {"label": "MID", "value": "MIDDLE"},
    {"label": "ADC", "value": "BOTTOM"},
    {"label": "SUP", "value": "UTILITY"},
]
TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]


# Dark DataTable styling for "Recent matches".
_DT_KW = dict(
    style_table={"overflowX": "auto"},
    style_header={"backgroundColor": "rgba(26,230,206,0.06)", "color": "#85A1AD",
                  "border": "none", "textTransform": "uppercase", "fontSize": "11px",
                  "letterSpacing": "0.05em", "fontWeight": "600"},
    style_cell={"backgroundColor": "transparent", "color": "#DEE9ED", "fontSize": "13px",
                "padding": "8px 10px", "border": "none",
                "borderBottom": "1px solid rgba(26,77,71,0.35)", "fontFamily": "Inter, sans-serif"},
    style_data_conditional=[{"if": {"state": "active"},
                             "backgroundColor": "rgba(26,230,206,0.06)", "border": "none"}],
)


def _field(label: str, control) -> html.Div:
    return html.Div([html.Label(label, className="field-label"), control],
                    style={"marginBottom": "18px"})


def _configure_tab() -> html.Div:
    return html.Div(style={"paddingTop": "22px"}, children=[
        # Macro/Game Analysis entry — also surfaced here so it's reachable from the
        # landing page (works without a loaded player via a pasted game ID).
        html.Div(style={"display": "flex", "justifyContent": "center", "marginBottom": "20px"},
                 children=dcc.Link(id="macro-link-cfg", className="ab-tab macro-tab",
                                   href="?view=macro", children=[
                                       html.Span("⚔", style={"marginRight": "8px"}),
                                       "Macro/Game Analysis"])),
        html.Div(className="ab-panel", children=[
            html.Div("Summoner Identity", className="section-h"),
            html.Div(style={"display": "flex", "gap": "16px", "flexWrap": "wrap"}, children=[
                html.Div(style={"flex": "2", "minWidth": "220px"}, children=_field(
                    "Riot ID", dcc.Input(id="riot-id-input", type="text",
                                         value="", placeholder="e.g. Faker"))),
                html.Div(style={"flex": "1", "minWidth": "140px"}, children=_field(
                    "Region", dcc.Input(id="region-input", type="text",
                                        value="", placeholder="e.g. NA1"))),
            ]),
            html.Div("Analysis Parameters", className="section-h"),
            html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr",
                            "gap": "20px 28px"}, children=[
                _field("Role Focus", dcc.RadioItems(
                    id="role-dropdown", options=ROLE_OPTIONS, value="BOTTOM",
                    className="role-radio", inline=True)),
                _field("Sample Size (games)", dcc.Slider(
                    id="count-input", min=10, max=300, step=10, value=25,
                    marks={10: "10", 100: "100", 200: "200", 300: "300"},
                    tooltip={"placement": "bottom", "always_visible": True})),
                _field("Queue Type", dcc.Dropdown(
                    id="queue-dropdown", clearable=False, value="both",
                    options=[{"label": "Ranked", "value": "ranked"},
                             {"label": "Unranked (practice)", "value": "unranked"},
                             {"label": "Both", "value": "both"}])),
                _field("Timeframe", dcc.Dropdown(
                    id="timeframe-dropdown", clearable=False, value=730,
                    options=[{"label": "All time", "value": 0},
                             {"label": "Last 7 days", "value": 7},
                             {"label": "Last 30 days", "value": 30},
                             {"label": "Last 90 days", "value": 90},
                             {"label": "Last 6 months", "value": 180},
                             {"label": "Last 1 year", "value": 365},
                             {"label": "Last 2 years", "value": 730}])),
                _field("Target Tier", dcc.Dropdown(
                    id="tier-dropdown", clearable=False, value=config.BENCHMARK_TIER,
                    options=[{"label": t.title(), "value": t} for t in TIERS])),
            ]),
            html.Div(style={"textAlign": "center", "marginTop": "10px"}, children=[
                dcc.Checklist(
                    id="force-refresh", className="force-refresh",
                    options=[{"label": " Refresh from Riot (re-pull instead of using saved data)",
                              "value": "force"}],
                    value=[], style={"marginBottom": "12px", "color": "#85A1AD",
                                     "fontSize": "13px"}),
                html.Button("Initiate Sequence", id="refresh-btn", n_clicks=0, className="ab-btn"),
                html.Div(id="refresh-status",
                         style={"color": "#85A1AD", "fontSize": "13px", "marginTop": "12px"}),
            ]),
        ]),
    ])


def _analysis_tab() -> html.Div:
    return html.Div(style={"paddingTop": "22px"}, children=[
        html.Div(id="backfill-banner"),  # background-pull banner + reload button
        # Itemization + Macro entry buttons — centered directly under the tab bar.
        # dcc.Link → client-side routing (no full reload) so player-store survives.
        # Itemization is revealed once a player is loaded; Macro is always shown
        # (it works without a player via a pasted game ID).
        html.Div(style={"display": "flex", "justifyContent": "center", "gap": "12px",
                        "flexWrap": "wrap", "marginBottom": "20px"}, children=[
            dcc.Link(id="itemization-link", className="ab-tab item-link", href="#",
                     style={"display": "none"}, children=[
                         html.Span("⚔", style={"marginRight": "8px"}), "Itemization Analysis"]),
            dcc.Link(id="macro-link", className="ab-tab macro-tab", href="?view=macro",
                     children=[html.Span("⚔", style={"marginRight": "8px"}),
                               "Macro/Game Analysis"]),
        ]),
        html.Div("High Signal Predictors", className="section-h"),
        html.Div("Your headline performance vs an equivalent target-tier player.",
                 className="section-sub"),
        dcc.Loading(html.Div(id="kpi-row",
                             style={"display": "flex", "gap": "14px", "flexWrap": "wrap"})),
        html.Div(id="readiness-panel", style={"margin": "18px 0"}),
        html.Div("Detailed Analytics", className="section-h"),
        html.Div("Per-game average for each high-signal metric vs the target tier, "
                 "with AI analysis of where to improve.", className="section-sub"),
        dcc.Loading(html.Div(id="detailed-metrics", className="ab-panel")),
        html.Div("AI Coaching", className="section-h"),
        dcc.Loading(html.Div(className="ab-panel coach-md",
                             children=dcc.Markdown(id="coach-feedback"))),
        html.Div("Recent Matches", className="section-h"),
        html.Div(className="ab-panel", children=dash_table.DataTable(
            id="matches-table", page_size=10, **_DT_KW)),
    ])


def serve_layout() -> html.Div:
    return html.Div(style={"maxWidth": "1000px", "margin": "0 auto",
                           "padding": "36px 24px 60px"}, children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="theme-store"),
        dcc.Store(id="player-store"),  # live-fetched player rows for the analysis
        dcc.Store(id="backfill-store"),  # background backfill {run_id, summoner, region}
        dcc.Store(id="nav-store"),        # {seq, target} drives post-Initiate navigation
        dcc.Store(id="cancelled", data=0),  # seq of the most recently cancelled request
        dcc.Interval(id="backfill-poll", interval=15000, n_intervals=0, disabled=True),
        # Full-screen "Working" overlay shown during the Initiate fetch.
        html.Div(id="working-overlay", className="working-overlay", style={"display": "none"},
                 children=html.Div(className="working-card", children=[
                     html.Div(className="working-spinner"),
                     html.Div("Working…", className="working-title"),
                     html.Div("Fetching games from Riot and computing your full "
                              "analysis — this usually takes 20–30 seconds.",
                              className="working-sub"),
                     html.Button("Cancel", id="cancel-btn", n_clicks=0,
                                 className="working-cancel"),
                 ])),
        html.Button(id="theme-toggle", className="theme-toggle", n_clicks=0, children=[
            html.Img(src=get_asset_url("sun.svg"), className="icon-sun", alt="Switch to light"),
            html.Img(src=get_asset_url("moon.svg"), className="icon-moon", alt="Switch to dark"),
        ]),
        html.H1("Abyssal Insight", className="ab-title"),
        html.Div("AI-Powered Tactical Analysis", className="ab-subtitle"),
        html.Div("Enter a Riot ID + Region on Configure, then Initiate Sequence",
                 id="player-line", className="ab-playerline"),
        # Main view: the Configure / Trend Analysis tabs (the Itemization + Macro
        # entry buttons sit centered under the tab bar, in the Trend Analysis tab).
        html.Div(id="main-view", children=[
            html.Div(style={"display": "flex", "justifyContent": "center", "margin": "8px 0 4px"},
                     children=dcc.Tabs(
                         id="view-tabs", value="configure", className="ab-tabs",
                         children=[
                             dcc.Tab(label="Configure", value="configure",
                                     className="ab-tab", selected_className="ab-tab--sel",
                                     children=_configure_tab()),
                             dcc.Tab(label="Trend Analysis", value="analysis",
                                     className="ab-tab", selected_className="ab-tab--sel",
                                     children=_analysis_tab()),
                         ])),
        ]),
        # URL-driven pages (hidden until their ?view= is set).
        _itemization_view(),
        _macro_view(),
    ])


def _macro_view() -> html.Div:
    return html.Div(id="macro-view", style={"display": "none"}, children=[
        html.Div(style={"display": "flex", "alignItems": "center",
                        "justifyContent": "space-between", "margin": "18px 0 6px"},
                 children=[
                     html.Div("Macro / Game Analysis", className="section-h",
                              style={"margin": "0"}),
                     html.A("← Back to Trend Analysis", id="macro-back", href="?",
                            className="item-back"),
                 ]),
        html.Div("Review a full game at the team level — objectives, vision, "
                 "teamfights, itemization vs threats, and more. Paste any game ID "
                 "(e.g. a game you want to review for your team), or pick one of your "
                 "recent games below.", className="section-sub"),
        html.Div(className="ab-panel", style={"display": "flex", "gap": "10px",
                                              "alignItems": "center", "flexWrap": "wrap"},
                 children=[
                     dcc.Input(id="macro-gameid-input", type="text",
                               placeholder="Paste a game ID, e.g. NA1_5580256027",
                               style={"flex": "1", "minWidth": "240px"}),
                     html.Button("Analyze", id="macro-analyze-btn", n_clicks=0,
                                 className="ab-btn", style={"padding": "10px 26px"}),
                 ]),
        html.Div("— or pick from your recent games —", className="section-sub",
                 style={"textAlign": "center", "margin": "8px 0"}),
        dcc.Loading(html.Div(id="macro-recent-list", className="ab-panel")),
        dcc.Loading(
            html.Div(id="macro-analysis"),
            delay_show=150,
            custom_spinner=html.Div(className="item-loading", children=[
                html.Div(className="item-spinner"),
                html.Div("Analyzing the game…", className="item-loading-title"),
                html.Div("Pulling the match + timeline from Riot and generating the "
                         "full team-level breakdown for both teams. This can take up "
                         "to a minute.", className="item-loading-sub"),
            ]),
        ),
    ])


def _itemization_view() -> html.Div:
    return html.Div(id="itemization-view", style={"display": "none"}, children=[
        html.Div(style={"display": "flex", "alignItems": "center",
                        "justifyContent": "space-between", "margin": "18px 0 6px"},
                 children=[
                     html.Div(id="item-header", className="section-h",
                              style={"margin": "0"}),
                     html.A("← Back to Trend Analysis", id="item-back", href="#",
                            className="item-back"),
                 ]),
        html.Div("Pick a game to break down its build. Each game ID is a link — "
                 "selecting one analyzes its itemization below.", className="section-sub"),
        dcc.Loading(html.Div(id="item-game-list", className="ab-panel")),
        # Per-game analysis makes 2 Riot calls + an LLM call (~30s). Show a clear,
        # themed "working" indicator instead of a blank panel while it runs.
        dcc.Loading(
            html.Div(id="item-analysis"),
            delay_show=150,  # don't flash on instant (cached) responses
            custom_spinner=html.Div(className="item-loading", children=[
                html.Div(className="item-spinner"),
                html.Div("Analyzing…", className="item-loading-title"),
                html.Div("Pulling the match detail + timeline from Riot and generating "
                         "the AI itemization verdict. This can take up to ~30 seconds.",
                         className="item-loading-sub"),
            ]),
        ),
    ])
