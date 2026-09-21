# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A League of Legends AI coach deployed as a **Databricks App** (Python/Dash). It
ingests Riot Games API data for a **target player** (chosen per-request via the
app's Riot ID + Region inputs — no hardcoded player) into Unity Catalog,
computes performance metrics, benchmarks them against the norms of a
**user-selected target rank** (the app's Target Tier picker — Iron→Diamond,
defaulting to Gold; not fixed to any single rank), and
uses the Databricks Foundation Model API (Claude) to generate coaching feedback.

## Product tenet (drives design decisions)

The coach's purpose is to make players **ready to climb in RANKED**. Both ranked
and unranked games are analyzed, with distinct roles:
- **Ranked** = the competitive arena that counts; performance here measures
  readiness for the next tier (`BENCHMARK_TIER` = the tier they're reaching for).
- **Unranked** = practice. The ideal player drills skills in many unranked games
  so they transfer into ranked advancement.

Concretely: skill metrics are computed across **both** queue types; the coaching
narrative frames unranked as practice, prescribes what to drill in unranked, and
judges whether the player is practicing enough (`metrics.readiness_summary`,
`MIN_PRACTICE_RATIO`). `config.queue_category` classifies each game
(ranked / unranked / other); the app shows a ranked-readiness panel. Keep new
features aligned with this tenet.

## Deployed instance (dev target, profile `default`)

Live at `https://league-ai-coach-3438839487639471.11.azure.databricksapps.com`.
Config baked into `databricks.yml`: warehouse `a3d6bc3533b85843` (ccrawford_xs),
serving endpoint `databricks-claude-sonnet-4-6`, secret scope `league_ai_coach`
(key `riot_api_key`), UC destination **`ccrawford.league_ai_coach`** (app + job
both target this — the app's SP has USE_CATALOG/USE_SCHEMA/SELECT on it). The
ingestion job's schedule is **PAUSED** (dev Riot key expires daily; run
on-demand via the app's Refresh button or `databricks bundle run ingest_matches`).

Serverless gotchas learned here (keep in mind): the spark_python_task exec's the
script with no `__file__` (jobs/ingest_matches.py walks up from cwd instead) and
takes no env vars (UC destination is passed as `--catalog/--schema` params, which
`config.set_destination` applies); `CREATE CATALOG` fails on the default-storage
metastore so `ensure_schema` only creates the schema; DAB `config.env` needs
`value_from` (snake_case) not `valueFrom`.

## Commands

```bash
# Tests — deterministic analysis layer only; no network or Spark needed.
pytest tests/                          # run all
pytest tests/test_metrics.py::test_evaluate_flags_weakness_below_threshold  # single test

# Run the Dash app locally (binds :8000)
pip install -r requirements.txt && python app/app.py

# Bundle (app + ingestion job): validate / deploy / run
databricks bundle validate -t dev --profile default
databricks bundle deploy   -t dev --profile default
databricks bundle run league_ai_coach -t dev --profile default   # the app
databricks bundle run ingest_matches  -t dev --profile default   # ingestion once
```

`tests/` and the app/job entry points prepend `src/` to `sys.path`, so imports
are top-level (`import config`, `from analysis import metrics`) — there is no
installed package. Match that convention in new modules.

## Architecture

**Fast live preview in the app + a background job for the full history, with
per-player tables.** The player shown is always exactly what was typed — never
stale/shared, because each player has their own table.

Flow (`app/callbacks.py::_initiate`):
1. **Synchronous preview.** `data_access.fetch_player_live` calls Riot directly
   (account-v1 → match-v5, paginated) with the app's `RIOT_API_KEY`, flattens via
   `participant_rows`, and stashes rows in `dcc.Store("player-store")`. Render
   callbacks compute `metrics.py` + `coach.py` (FMAPI) from the store — role/tier
   changes are instant. Bounded by `MAX_SYNC_MATCHES` (~90) for the Apps 120s
   proxy timeout; fails fast on a 429 (`RiotClient(max_429_wait=…)`). The app
   needs outbound internet to Riot (checked at startup by `app.py::_egress_check`).
2. **Background backfill for long windows.** If the timeframe is > 30 days, the
   preview pulls only the **last 30 days** (fast), then `data_access.trigger_backfill`
   runs the job (`--mode summoner`) for the **full** window, which writes the
   player's own Delta table `config.summoner_table(name, region)` (e.g.
   `…summoners_rift_baconandeggsusa_na1`) via `pipeline.build_summoner_table`. A
   **banner** explains this; a `dcc.Interval` polls `data_access.job_run_status`;
   when the job finishes, the banner shows a **"Reload for full analysis"** button
   that is just an `<a href="?summoner=…&region=…">`. On reload, `_initiate`'s
   URL branch reads the per-player table (`load_summoner_table`) and renders the
   full history. ≤ 30 days = synchronous only, no job.
3. **Benchmark cohort** comes from UC (`gold_rank_benchmarks`,
   `gold_challenge_benchmarks`), seeded occasionally by the job in `--mode cohort`
   (`run_cohort`). The old shared `gold_player_performance` medallion path is
   dormant (replaced by per-player tables); `trigger_refresh`/`validate_riot_id`
   linger unused.

### App look & views (Abyssal Insight theme)

The UI is themed after a Replit design ("Abyssal Insight" — Cinzel display +
Inter body). The theme lives entirely in `app/assets/theme.css` (Dash auto-serves
`app/assets/`) plus inline style dicts in `layout.py` — the backend is unchanged.
A **light/dark toggle** (top-right, `sun.svg`/`moon.svg`) flips a `light` class on
`<body>` via a clientside callback; all colors are CSS variables overridden under
`body.light`, and the backdrop swaps between `dark-rift.png` (dark) and
`light-rift.png` (light). Keep new colors as `var(--...)` (or theme-aware) so both
modes work — hardcoded hex in inline styles won't adapt. Two `dcc.Tabs`:

- **Configure** — required **Riot ID** + **Region** text inputs (the Region is
  the tagLine — often custom, e.g. `NA1`/`KR1`/`Tech`/`666`, so it's free text
  not a fixed list), role focus, sample size slider, queue type, timeframe,
  target tier, and the "Initiate Sequence" button (= the refresh; jumps to
  Analysis). Refresh passes `--game-name/--tag-line/--platform/--region` to the
  job (`config.set_target` + `set_routing`); the Region string doubles as the
  tagLine and selects routing via `config.routing_for` (exact region code, then
  tag-prefix match like `KR1`→kr/asia, else NA). The target tier drives the
  benchmark comparison. Verified end-to-end: ingesting a different Riot ID
  replaces the single `gold_player_performance` table with that player.

**Analyzing a different player:** Initiate Sequence fetches that player's games
live (`fetch_player_live`) and renders them synchronously — the header ("Analyzing
X#Y") and all panels come straight from the just-fetched rows in `player-store`,
so the player shown is always exactly what was typed (no stale/shared state). A
404 (or unreachable Riot) returns a user-facing error and stays on Configure.
There is no hardcoded player and no async wait.

**Asset-URL gotcha:** in `theme.css` the rift backgrounds use **relative** URLs
(`url("dark-rift.png")`), not `/assets/...` — an absolute path breaks if the app
is served under a path prefix, which made the dark backdrop not render on the
deployed app. For component `src` use `dash.get_asset_url(...)` for the same
reason. Background visibility is tuned via the `body::before` opacity + the
`body::after` gradient (keep the dark overlay light enough to see the forest).
- **Analysis** — High Signal Predictors (KPI cards), ranked-readiness verdict,
  Detailed Analytics (the 20 `challenges` metrics: per-game avg, cohort avg,
  delta, one-line AI analysis via `coach.analyze_metrics`), coaching narrative,
  recent matches. Both analysis callbacks gate on `tab == "analysis"` so the two
  LLM calls only fire when that tab is open.

The 20 metrics come from `config.CHALLENGE_METRICS`.

- **Itemization page** — a separate URL-driven view (not a tab) entered via the
  centered **"Itemization Analysis"** button, which only appears on the Analysis
  view once a player is loaded (`callbacks._itemization_link` sets its href +
  visibility from `player-store` + `view-tabs`). The page is toggled by
  `?view=itemization&s=<name>&r=<region>[&m=<matchId>]` on the `url` Location:
  `_item_view` swaps `main-view`↔`itemization-view` and renders the player's
  **last 10 cached games** (from `load_summoner_table`) as a picker where each
  **game ID is a link** (`&m=<matchId>`). Selecting one re-renders `_item_analysis`
  for that game. Per-game analysis (`data_access.fetch_itemization`) makes 2 Riot
  calls — match detail (final `item0-6` for the player + the lane opponent, found
  by same `teamPosition` / opposing `teamId`) and the **timeline** (starting items
  + undo/sold-adjusted purchase order, via `analysis.itemization`) — resolves item
  IDs to names + icons through **Data Dragon** (`app/ddragon.py`, public CDN, no
  key, cached per-process), and gets a counter-build verdict from
  `coach.analyze_itemization` (FMAPI; templated fallback). With **no game
  selected** the same panel shows **Build Trends** (`data_access.fetch_item_trends`,
  last ~6 games): each final build is classified via Data Dragon item tags
  (`ddragon.classify_item` — boots/armor/MR=`SpellBlock`/anti-heal-from-description/
  leftover-component), aggregated (`itemization.aggregate_trends`) into rates +
  most-common opening buy, and narrated by `coach.analyze_item_trends`. With a
  game selected the panel also shows a **Game Timeline** (`analysis.timeline` from
  the match timeline already fetched: gold-lead-vs-lane-opponent curve with death
  markers via `dcc.Graph`/plotly, plus gold/CS lead @10/@15 and K/D/A). All FMAPI
  calls route through a cached `coach._chat` (keyed on prompt+params) so re-renders
  don't re-bill the endpoint. The deterministic layers in
  `src/analysis/{itemization,timeline}.py` and the cache are unit-tested
  (`test_itemization.py`, `test_timeline.py`, `test_llm_cache.py`); Data Dragon /
  Riot calls are not.

- **Macro / Game Analysis page** — a separate URL-driven view (`?view=macro&...[&m=]`)
  entered via a **tab-styled link in the tab row** (`callbacks._macro_link`, reachable
  from anywhere — no player required). The Analysis tab is labelled **"Individual
  Analysis"**; macro is the team/game-level complement. Pick one of your recent games
  (same picker as itemization, `view="macro"`) **or paste any game ID** (a clientside
  callback navigates to `?view=macro&m=<id>` — useful for a coach reviewing a team's
  game). `data_access.fetch_macro(region, match_id, focus_puuid)` routes off the
  match-id prefix (so any region works), pulls match detail + timeline, runs the
  deterministic team layer `src/analysis/macro.py` (objectives + timings/soul/trades,
  gold/XP swing curve, vision, damage profiles → AD/AP threats, lane outcomes @14,
  teamfight + death/trade clustering, and low-confidence positional reads for
  support-roam / split-push), adds Data Dragon champion icons + a resistance
  assessment (armor/MR built vs the enemy's biggest AD/AP threats), and
  `coach.analyze_macro` narrates it (FMAPI; templated fallback). For the user's own
  games the player's team is the focus; a pasted id gives a neutral both-teams review.
  `_macro_panel` renders both team cards, the gold-lead chart, lane table, teamfight
  summary, and the AI narrative. The deterministic layer is unit-tested
  (`test_macro.py`). **Gotcha:** a childless Dash component (e.g. `dcc.Graph`) is
  falsy via `__len__`, so use `x if x is not None else fallback`, never `x or fallback`.

**SQL connection gotcha (learned here):** Dash runs callbacks on separate
threads and a Databricks SQL connection is not safe to share across them — a
single cached connection caused panels to randomly render empty. `data_access`
now keeps a **thread-local** connection with reconnect+retry (also smooths SQL
warehouse cold starts).

The 20 metrics are extracted into `silver_match_participants` (one float column
each, coerced in `models.participant_rows`), projected into
`gold_player_performance`, and aggregated by `pipeline.build_challenge_benchmarks`
into the **tall** `gold_challenge_benchmarks` table
(tier, team_position, metric, gold_avg, sample_size) read via
`data_access.load_challenge_benchmarks` → `{role: {metric: avg}}`.

### Pull controls (UI toggles → job)

The app exposes **queue mode** (ranked / unranked / both), **how many games**,
and a **timeframe**. On refresh these flow as Jobs API `python_params`
(`--queue-mode/--count/--start-time`) → argparse in `jobs/ingest_matches.py` →
`pipeline.run(...)`. Queue mode maps to Riot match-v5 `type` via
`config.riot_match_types`: ranked→["ranked"], unranked→["normal"],
both→["ranked","normal"] (excludes ARAM/bot; "both" merges the two id lists and
keeps the most-recent `count` by `gameCreation`). The timeframe (last N days) is
converted to an epoch `start_time` in the callback. `build_gold_player` is
bounded to the most-recent `count` matches so the analysis window matches the
pull even though bronze accumulates over time. Cohort seeding
(`ingest_cohort`/`run_cohort`) stays ranked-solo (queue 420) for a clean
GOLD benchmark.

Key design rule: **the LLM only narrates numbers computed in `metrics.py`** — it
is never asked to produce statistics. `coach.py` and `metrics.py` both have
deterministic fallbacks (templated narrative / static Gold benchmarks) so the
app stays functional with no serving endpoint and before opponent-tier data
exists.

## Conventions & constraints

- **Secrets**: the Riot API key is read from the `league_ai_coach` secret scope
  (`riot_api_key`), injected as `RIOT_API_KEY`. Never hardcode it or any
  workspace ID — all IDs come from env vars via `valueFrom` in `app.yaml` /
  `databricks.yml`. Riot **dev keys expire every 24 hours**.
- **Riot routing**: `na1` platform routing for league-v4/summoner-v4; `americas`
  region routing for account-v1/match-v5. See `src/config.py`.
- **Databricks Apps platform**: app must bind `0.0.0.0:$DATABRICKS_APP_PORT`
  (hardcoding a port/localhost causes 502s); 10 MB/file limit (no `node_modules`
  or bundled deps); only `requirements.txt` is supported; resources declared in
  `databricks.yml` get permissions auto-granted to the app service principal.
- `pyspark` is intentionally absent from `requirements.txt` — it's provided by
  the job cluster and unused by the app process.

## Opponent-tier classification

Implemented across the pipeline: `build_player_ranks` caches each distinct
participant's ranked tier (league-v4) into `silver_player_ranks`, fetching only
puuids not already cached. `build_gold_player` joins the **lane opponent**
(same `team_position`, different `team_id`) and attaches `opponent_tier` so the
app isolates games vs the benchmark cohort. `build_gold_benchmarks` averages
metrics by `(tier, team_position)` into `gold_rank_benchmarks`; the app prefers
these live benchmarks and falls back to `metrics.DEFAULT_GOLD_BENCHMARKS` only
when the table is empty.

The Spark-dependent pipeline functions are not unit-tested (no local Spark); the
deterministic analysis layer in `metrics.py` is. The full Riot→bronze path plus
the league-v4 rank lookup were validated live against `NA1` data.
