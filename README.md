# League AI Coach

A League of Legends AI coach, hosted as a **Databricks App**. It ingests recent
games for a **target player** (chosen in the app via Riot ID + Region — no
hardcoded player) from the Riot Games API into Unity Catalog,
computes performance metrics, benchmarks them against the norms of a
**rank the user chooses** (the Target Tier picker — Iron→Diamond, default Gold —
so you measure yourself against whatever rank you're aiming for), and
uses the Databricks Foundation Model API (Claude) to generate prioritized,
actionable coaching feedback.

## Tenet: ranked readiness

The coach's goal is to get players **ready to climb in RANKED**. It analyzes both
ranked and unranked games, treating **unranked as practice** — the ideal player
drills skills in many unranked games so they carry into ranked advancement. The
app therefore (1) measures skills across both queue types, (2) benchmarks against
the tier the player is reaching for, and (3) coaches on what to practice in
unranked and whether they're practicing enough to climb.

## Architecture

```
Riot API ──▶ ingestion job (Spark) ──▶ Unity Catalog (medallion)
 (match-v5,        jobs/ingest_matches.py     bronze_matches
  account-v1,      src/ingest/pipeline.py     silver_match_participants
  league-v4)                                  gold_player_performance
                                              gold_rank_benchmarks
                                                     │
                          ┌──────────────────────────┘
                          ▼
   Dash app (app/) ──▶ src/analysis/metrics.py  (stats vs the chosen target rank)
                   ──▶ src/analysis/coach.py     (FMAPI Claude narrative)
                   ──▶ "Refresh" button triggers the ingestion job (Jobs API)
```

- **`src/riot/`** — Riot API client (auth, host routing, rate limiting, retries)
  and JSON→row flattening.
- **`src/ingest/`** — medallion pipeline (bronze→silver→gold). `jobs/ingest_matches.py`
  is the job entry point; it backs both the **scheduled** run (cron in
  `databricks.yml`) and the **on-demand** refresh (triggered from the app).
- **`src/analysis/`** — `metrics.py` computes exact stats and compares to cohort
  benchmarks; `coach.py` turns that comparison into narrative via FMAPI (falls
  back to a templated summary when no endpoint is configured).
- **`app/`** — Dash UI reading gold tables via the SQL warehouse.

## Setup

1. **Riot key as a secret** (never commit it; dev keys expire daily):
   ```bash
   databricks secrets create-scope league_ai_coach --profile default
   databricks secrets put-secret league_ai_coach riot_api_key --profile default
   ```
2. **Fill `databricks.yml` variables** — `warehouse_id`, `serving_endpoint`,
   `secret_scope`.
3. **Local dev**: `cp .env.example .env`, fill `RIOT_API_KEY`, then run tests / app.

## Common commands

```bash
# Tests (deterministic analysis layer, no network/Spark)
pytest tests/

# Run the Dash app locally
pip install -r requirements.txt && python app/app.py   # http://localhost:8000

# Validate & deploy the bundle (app + job)
databricks bundle validate -t dev --profile default
databricks bundle deploy   -t dev --profile default
databricks bundle run league_ai_coach -t dev --profile default

# Run ingestion once on demand
databricks bundle run ingest_matches -t dev --profile default
```

## Notes / TODO

- **Opponent tier classification** (`build_gold_benchmarks`) is a marked stub:
  isolating games against Gold opponents requires a league-v4 call per unique
  opponent, which is heavy under a dev key. Until enabled, the analysis uses the
  static Gold reference benchmarks in `src/analysis/metrics.py`.
- Rotate the Riot key regularly; request a production key for a hosted app.
