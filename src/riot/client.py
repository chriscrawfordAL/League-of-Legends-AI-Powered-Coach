"""Thin client for the Riot Games LoL APIs used by the coach.

Covers the four endpoints the pipeline needs:
  * account-v1   resolve Riot ID -> puuid                       (REGION routing)
  * match-v5     list match ids, fetch full match              (REGION routing)
  * league-v4    rank entries for a puuid (to classify cohort) (PLATFORM routing)

The client handles auth headers, host routing, rate limiting, and 429/5xx
retries. It performs NO live calls at import time — construct it explicitly.

Docs: https://developer.riotgames.com/apis
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .rate_limiter import RateLimiter


class RiotAPIError(RuntimeError):
    """Raised for non-retryable Riot API responses."""


class RiotClient:
    def __init__(
        self,
        api_key: str,
        platform: str = "na1",
        region: str = "americas",
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 3,
        timeout: float = 10.0,
        max_429_wait: float = 60.0,
    ):
        if not api_key:
            # Fail loud rather than make an obviously-unauthorized call.
            raise ValueError(
                "Riot API key is empty. Set RIOT_API_KEY (locally) or wire the "
                "'riot_api_key' secret in app.yaml / databricks.yml."
            )
        self._key = api_key
        self.platform = platform
        self.region = region
        self._rl = rate_limiter or RateLimiter()
        self._max_retries = max_retries
        self._timeout = timeout
        self._max_429_wait = max_429_wait
        self._session = requests.Session()
        self._session.headers.update({"X-Riot-Token": api_key})

    # -- low-level ---------------------------------------------------------
    def _get(self, host: str, path: str, params: dict | None = None) -> Any:
        url = f"https://{host}.api.riotgames.com{path}"
        for attempt in range(self._max_retries + 1):
            self._rl.acquire()
            resp = self._session.get(url, params=params, timeout=self._timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                # Fail fast rather than block a synchronous request (and the app's
                # 120s proxy) when the key is rate-limited for a long window.
                if retry_after > self._max_429_wait or attempt >= self._max_retries:
                    raise RiotAPIError(
                        f"429 rate limited (retry after {retry_after:.0f}s) for {url}")
                time.sleep(retry_after)
                continue
            if 500 <= resp.status_code < 600 and attempt < self._max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RiotAPIError(f"{resp.status_code} for {url}: {resp.text[:300]}")
        raise RiotAPIError(f"Exhausted retries for {url}")

    # -- account-v1 (REGION) ----------------------------------------------
    def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Resolve a Riot ID (gameName#tagLine) to an account incl. puuid."""
        return self._get(
            self.region,
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
        )

    # -- match-v5 (REGION) -------------------------------------------------
    def get_match_ids(
        self,
        puuid: str,
        start: int = 0,
        count: int = 20,
        queue: int | None = None,
        type_: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[str]:
        """Recent match ids (newest first), optionally filtered.

        ``queue`` (a numeric queueId) and ``type_`` (a category: "ranked",
        "normal", "tourney", "tutorial") are mutually exclusive per Riot.
        ``start_time``/``end_time`` are epoch seconds (match-v5 only returns
        games on/after 2021-06-16 when start_time is set).
        """
        params: dict[str, Any] = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        if type_ is not None:
            params["type"] = type_
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return self._get(
            self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params
        )

    def get_match(self, match_id: str) -> dict:
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}")

    def get_match_timeline(self, match_id: str) -> dict:
        """Per-minute frames + events for a match (ITEM_PURCHASED/SOLD/UNDO, etc.).

        Used to reconstruct a player's starting items and full purchase order,
        which the final item0-6 slots on the match payload don't preserve.
        """
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}/timeline")

    # -- league-v4 (PLATFORM) ---------------------------------------------
    def get_league_entries_by_puuid(self, puuid: str) -> list[dict]:
        """Ranked entries (tier/division/LP per queue) for a player.

        Used to classify opponents into a tier so we can isolate games played
        against the benchmark cohort (e.g. GOLD).
        """
        return self._get(
            self.platform, f"/lol/league/v4/entries/by-puuid/{puuid}"
        )

    def get_league_entries_by_tier(
        self,
        tier: str,
        division: str,
        queue: str = "RANKED_SOLO_5x5",
        page: int = 1,
    ) -> list[dict]:
        """Enumerate ranked players at a given tier+division (paginated).

        This is how we discover a cohort (e.g. GOLD) without knowing any
        usernames. Entries include summonerId and, on current API versions,
        puuid; use :meth:`get_summoner_by_id` to resolve puuid otherwise.
        """
        return self._get(
            self.platform,
            f"/lol/league/v4/entries/{queue}/{tier}/{division}",
            {"page": page},
        )

    # -- summoner-v4 (PLATFORM) -------------------------------------------
    def get_summoner_by_id(self, summoner_id: str) -> dict:
        """Resolve an encrypted summonerId to a summoner (incl. puuid)."""
        return self._get(
            self.platform, f"/lol/summoner/v4/summoners/{summoner_id}"
        )
