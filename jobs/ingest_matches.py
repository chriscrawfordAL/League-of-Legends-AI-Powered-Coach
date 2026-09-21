"""Entry point for the scheduled / on-demand ingestion job.

Deployed and scheduled via databricks.yml as a spark_python_task. The same
script backs both ingestion modes the project uses:
  * scheduled    -> the job's cron trigger
  * on-demand    -> the Dash app calls Jobs API run-now on this job, passing the
                    UI toggles as python_params (--queue-mode/--count/--start-time)

The Riot API key is read from the `league_ai_coach` secret scope so it never
lives in source. dbutils is available in the job runtime.
"""

import argparse
import os
import sys


def _add_src_to_path() -> None:
    """Put the `src` source root on sys.path.

    Robust across contexts: local run (``__file__`` defined) and Databricks
    serverless spark_python_task (script is exec'd, so ``__file__`` is NOT
    defined). Walks up from candidate starting points looking for src/config.py.
    """
    starts = [os.getcwd()]
    try:
        starts.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass  # serverless exec context
    for start in starts:
        d = start
        for _ in range(6):
            cand = os.path.join(d, "src")
            if os.path.exists(os.path.join(cand, "config.py")):
                sys.path.insert(0, cand)
                return
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    raise RuntimeError(f"Could not locate src/ (cwd={os.getcwd()})")


_add_src_to_path()

import config  # noqa: E402
from ingest import pipeline  # noqa: E402
from riot.client import RiotClient  # noqa: E402


def _api_key() -> str:
    # Prefer a real secret; fall back to env for local `databricks bundle run`.
    key = os.environ.get("RIOT_API_KEY", "")
    if key:
        return key
    try:
        from pyspark.dbutils import DBUtils  # type: ignore
        from pyspark.sql import SparkSession

        dbutils = DBUtils(SparkSession.getActiveSession())
        return dbutils.secrets.get(scope="league_ai_coach", key="riot_api_key")
    except Exception as exc:  # pragma: no cover - runtime-only path
        raise RuntimeError(
            "No Riot API key available. Set the RIOT_API_KEY env or create the "
            "'league_ai_coach' secret scope with key 'riot_api_key'."
        ) from exc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="League AI Coach ingestion")
    p.add_argument("--queue-mode", choices=["ranked", "unranked", "both"],
                   default=config.QUEUE_MODE)
    p.add_argument("--count", type=int, default=config.MATCH_FETCH_COUNT)
    p.add_argument("--start-time", type=int, default=None,
                   help="epoch seconds; omit for no timeframe")
    p.add_argument("--end-time", type=int, default=None, help="epoch seconds")
    p.add_argument("--refresh-mode", choices=["fresh", "incremental"], default="fresh",
                   help="fresh = wipe+re-pull the player's window; incremental = append new")
    p.add_argument("--mode", choices=["summoner", "cohort"], default="summoner",
                   help="summoner = backfill one player's per-player table; "
                        "cohort = (re)seed the tier benchmark tables")
    # Serverless tasks can't take env vars, so the UC destination is passed in.
    p.add_argument("--catalog", default=config.UC_CATALOG)
    p.add_argument("--schema", default=config.UC_SCHEMA)
    # Analyzed player (the app's Riot ID input) + region (drives tagLine + routing).
    p.add_argument("--game-name", default=config.TARGET_GAME_NAME)
    p.add_argument("--tag-line", default=config.TARGET_TAG_LINE)
    p.add_argument("--platform", default=config.PLATFORM)
    p.add_argument("--region", default=config.REGION)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    from pyspark.sql import SparkSession

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    config.set_destination(args.catalog, args.schema)
    config.set_target(args.game_name, args.tag_line)
    config.set_routing(args.platform, args.region)
    spark = SparkSession.builder.getOrCreate()
    client = RiotClient(
        api_key=_api_key(), platform=config.PLATFORM, region=config.REGION
    )
    if args.mode == "cohort":
        summary = pipeline.run_cohort_all(spark, client)
        print(f"[league_ai_coach] all-tier benchmark seed complete: {summary}")
        return

    # Default: background backfill of one player's per-player table.
    pipeline.ensure_schema(spark)
    summary = pipeline.build_summoner_table(
        spark, client, args.game_name, args.tag_line, args.tag_line,
        count=args.count, start_time=args.start_time, queue_mode=args.queue_mode)
    print(f"[league_ai_coach] summoner backfill complete ({args.game_name}#"
          f"{args.tag_line}, {args.count} games): {summary}")


if __name__ == "__main__":
    main()
