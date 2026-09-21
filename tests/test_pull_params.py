"""Tests for the pull-parameter plumbing (queue mode, count, timeframe)."""

import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

import config  # noqa: E402


def test_riot_match_types_mapping():
    assert config.riot_match_types("ranked") == ["ranked"]
    assert config.riot_match_types("unranked") == ["normal"]
    assert config.riot_match_types("both") == ["ranked", "normal"]


def test_riot_match_types_defaults_to_both():
    assert config.riot_match_types("nonsense") == ["ranked", "normal"]
    assert config.riot_match_types(None) == config.riot_match_types(config.QUEUE_MODE)


def test_summoner_table_naming():
    assert config.summoner_table("BaconAndEggsUSA", "NA1").endswith(
        ".summoners_rift_baconandeggsusa_na1")
    # spaces/punctuation are stripped
    assert config.summoner_table("Last Starfighter", "NA2").endswith(
        ".summoners_rift_laststarfighter_na2")
    assert config.summoner_table("Faker#$%", "KR1").endswith(
        ".summoners_rift_faker_kr1")


def test_routing_for_regions():
    assert config.routing_for("NA1") == ("na1", "americas")
    assert config.routing_for("NA2") == ("na1", "americas")  # custom tag, NA platform
    assert config.routing_for("KR") == ("kr", "asia")
    assert config.routing_for("euw1") == ("euw1", "europe")  # case-insensitive
    assert config.routing_for("OC1") == ("oc1", "sea")
    # free-text tags that aren't exact region codes route by prefix
    assert config.routing_for("KR1") == ("kr", "asia")
    assert config.routing_for("euw") == ("euw1", "europe")
    assert config.routing_for("bogus") == ("na1", "americas")  # safe default


class _FakeClient:
    """Minimal client returning a sliceable id list, recording call offsets."""
    def __init__(self, total):
        self._ids = [f"NA1_{i}" for i in range(total)]
        self.calls = []

    def get_match_ids(self, puuid, start=0, count=20, type_=None,
                      start_time=None, end_time=None):
        self.calls.append((start, count))
        return self._ids[start:start + count]


def test_paged_match_ids_pages_past_100():
    from ingest import pipeline
    c = _FakeClient(250)
    ids = pipeline._paged_match_ids(c, "puuid", 250, "ranked", None, None)
    assert len(ids) == 250
    # 100-cap per request: pages of 100, 100, 50
    assert c.calls == [(0, 100), (100, 100), (200, 50)]


def test_paged_match_ids_stops_when_history_runs_out():
    from ingest import pipeline
    c = _FakeClient(80)  # only 80 games exist
    ids = pipeline._paged_match_ids(c, "puuid", 300, "ranked", None, None)
    assert len(ids) == 80  # stops early, no infinite loop


def _load_job_module():
    path = os.path.join(ROOT, "jobs", "ingest_matches.py")
    spec = importlib.util.spec_from_file_location("ingest_matches_job", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_job_argparse_reads_ui_params():
    job = _load_job_module()
    args = job._parse_args(["--queue-mode", "both", "--count", "25"])
    assert args.queue_mode == "both"
    assert args.count == 25
    assert args.start_time is None
    assert args.refresh_mode == "fresh"  # default


def test_job_argparse_refresh_mode():
    job = _load_job_module()
    args = job._parse_args(["--refresh-mode", "incremental"])
    assert args.refresh_mode == "incremental"


def test_job_argparse_mode():
    job = _load_job_module()
    assert job._parse_args([]).mode == "summoner"  # default
    assert job._parse_args(["--mode", "cohort"]).mode == "cohort"


def test_job_argparse_timeframe():
    job = _load_job_module()
    args = job._parse_args(["--queue-mode", "ranked", "--count", "10",
                            "--start-time", "1700000000"])
    assert args.queue_mode == "ranked"
    assert args.start_time == 1700000000
