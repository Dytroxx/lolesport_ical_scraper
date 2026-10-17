"""Unoffizielle LoL Esports API für Schedules.

Hauptquelle: https://esports-api.lolesports.com/persisted/gw
Falls die API nicht erreichbar ist, wird auf den HTML-Parser (scrape.py)
zurückgegriffen.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from zoneinfo import ZoneInfo

from .models import Match
from .util import Fetcher, isoformat_z, stable_uid

logger = logging.getLogger(__name__)

API_BASE = "https://esports-api.lolesports.com/persisted/gw"

API_KEYS = [
    "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z",
]


@dataclass(frozen=True)
class ApiConfig:
    tz: str = "Europe/Berlin"
    cache_dir: str = ".cache/lolesports_ical"
    cache_ttl: int = 120
    rate_limit_s: float = 0.15
    timeout_s: float = 10.0
    days: int = 30
    result_lookback_days: int = 7


def _get_api_keys() -> List[str]:
    from os import environ

    env_keys = environ.get("LOL_API_KEYS", "").strip()
    if env_keys:
        keys = [k.strip() for k in env_keys.split(",") if k.strip()]
        if keys:
            return keys
    return list(API_KEYS)


def _validate_response(resp: httpx.Response) -> Dict[str, Any]:
    """API response validieren und JSON parsen."""
    status = resp.status_code

    if status == 401:
        raise RuntimeError("[api] 401 Unauthorized – API-Key ungültig oder rotiert.")
    if status == 403:
        raise RuntimeError("[api] 403 Forbidden – API-Key abgelaufen oder rotiert.")
    if status == 400:
        raise RuntimeError(f"[api] 400 Bad Request: {resp.text[:200]}")
    if status == 404:
        raise RuntimeError("[api] 404 Not Found – Endpunkt nicht verfügbar")
    if status == 499:
        raise RuntimeError("[api] 499 Client Closed Request")

    if status == 429:
        retry_after = resp.headers.get("Retry-After", "unbekannt")
        logger.warning(f"[api] 429 Too Many Requests (Retry-After: {retry_after})")

    error_msgs = {
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
        520: "Cloudflare-Serverfehler",
        521: "Web Server Down",
        522: "Connection Timed Out",
        523: "Origin Unreachable",
        524: "A Timeout Occurred",
        525: "SSL Handshake Failed",
        526: "Invalid SSL Certificate",
        527: "Railgun Error",
        528: "Origin Connection Timed Out",
    }
    if status in error_msgs:
        raise RuntimeError(f"[api] {status} {error_msgs[status]}")
    if 598 <= status <= 599:
        raise RuntimeError(f"[api] {status} Network Read Timeout")
    if 500 <= status < 600:
        raise RuntimeError(f"[api] {status} Server Error")

    if status >= 400:
        raise RuntimeError(f"[api] HTTP {status} – {resp.text[:300]}")

    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"[api] JSON Decode Error: {resp.text[:200]}")

    if "data" not in data:
        raise RuntimeError("[api] Missing 'data' key")
    if "schedule" not in data.get("data", {}):
        raise RuntimeError("[api] Missing 'data.schedule' key")

    return data


class LolEsportsAPIClient:
    """Holt Events von der unofficial LoL Esports API."""

    def __init__(self, config: ApiConfig, fetcher: Fetcher, scrape_matches_func):
        self.config = config
        self.fetcher = fetcher
        self.scrape_matches_func = scrape_matches_func

    def fetch_matches(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        try:
            return self._fetch_via_api(league_slugs)
        except Exception as exc:
            print(f"[api] API fetch failed ({exc}), falling back to HTML parser")
            return self._fetch_via_html(league_slugs)

    def _fetch_via_api(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        now = datetime.now(timezone.utc)
        lower = now - timedelta(days=self.config.result_lookback_days)
        upper = now + timedelta(days=self.config.days)

        def fetch_page(token: str | None = None) -> Dict[str, Any]:
            params = {"hl": "en-US"}
            if token:
                params["pageToken"] = token
            response = self.fetcher.get(
                f"{API_BASE}/getSchedule",
                params=params,
                headers={"x-api-key": _get_api_keys()[0]},
            )
            return _validate_response(response)["data"]["schedule"]

        first = fetch_page()
        all_events = list(first.get("events", []))
        for direction in ("newer", "older"):
            page = first
            seen_tokens: set[str] = set()
            while True:
                dates = []
                for event in page.get("events", []):
                    try:
                        dates.append(
                            datetime.fromisoformat(
                                event["startTime"].replace("Z", "+00:00")
                            ).astimezone(timezone.utc)
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                if dates and (
                    (direction == "newer" and max(dates) >= upper)
                    or (direction == "older" and min(dates) <= lower)
                ):
                    break
                token = page.get("pages", {}).get(direction)
                if not token:
                    break
                if token in seen_tokens:
                    raise RuntimeError("[api] Repeated pagination token")
                seen_tokens.add(token)
                page = fetch_page(token)
                all_events.extend(page.get("events", []))

        print(f"[api] Fetched {len(all_events)} raw events from API")
        if not all_events:
            logger.warning("[api] API returned 0 events, falling back to HTML parser")
            return self._fetch_via_html(league_slugs)
        return self._parse_events(all_events, league_slugs or [])

    def _fetch_via_html(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        return self.scrape_matches_func(
            league_slugs=league_slugs or [],
            fetcher=self.fetcher,
        )

    def _parse_events(self, events: List[Dict[str, Any]], allowed: List[str]) -> List[Match]:
        tz = ZoneInfo(self.config.tz)
        matches: List[Match] = []

        for event in events:
            if event.get("type") != "match":
                continue

            match_data = event.get("match")
            if not match_data:
                continue

            teams = match_data.get("teams", [])
            if len(teams) < 2:
                continue

            league = event.get("league", {})
            slug = league.get("slug") or ""
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug) or (allowed and slug not in allowed):
                continue

            start_str = event.get("startTime", "")
            if not start_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            t1 = teams[0]
            t2 = teams[1]
            team1_name = t1.get("name") or t1.get("code") or "TBD"
            team2_name = t2.get("name") or t2.get("code") or "TBD"
            team1_code = t1.get("code") or None
            team2_code = t2.get("code") or None

            r1 = t1.get("result") or {}
            r2 = t2.get("result") or {}
            team1_score = int(r1["gameWins"]) if r1.get("gameWins") is not None else None
            team2_score = int(r2["gameWins"]) if r2.get("gameWins") is not None else None

            winner = None
            if r1.get("outcome") == "win":
                winner = team1_name
            elif r2.get("outcome") == "win":
                winner = team2_name

            strategy = match_data.get("strategy") or {}
            best_of = None
            if strategy.get("type") == "bestOf":
                count = strategy.get("count")
                if count is not None:
                    best_of = f"Bo{int(count)}"

            state = event.get("state", "unstarted")
            if state not in ("unstarted", "inProgress", "completed"):
                state = "unstarted"

            match_id = match_data.get("id")
            match_url = (
                f"https://lolesports.com/live/{slug}/{match_id}"
                if match_id
                else f"https://lolesports.com/schedule?leagues={slug}"
            )

            uid = stable_uid(
                league_slug=slug,
                match_id=str(match_id) if match_id else None,
                match_start_utc_iso=isoformat_z(start_dt),
                team1=team1_name,
                team2=team2_name,
                stage=event.get("blockName"),
            )

            matches.append(
                Match(
                    league_slug=slug,
                    league_name=league.get("name", slug),
                    match_id=str(match_id) if match_id else None,
                    match_start_utc=start_dt,
                    match_start_local=start_dt.astimezone(tz),
                    best_of=best_of,
                    team1=team1_name,
                    team2=team2_name,
                    team1_code=team1_code,
                    team2_code=team2_code,
                    stage=event.get("blockName"),
                    match_url=match_url,
                    stable_uid=uid,
                    state=state,
                    team1_score=team1_score,
                    team2_score=team2_score,
                    winner=winner,
                )
            )

        seen: Dict[str, Match] = {}
        for m in matches:
            seen[m.stable_uid] = m
        return list(seen.values())


def api_fetch_leagues(fetcher: Fetcher) -> List[Dict[str, Any]]:
    response = fetcher.get(
        f"{API_BASE}/getLeagues",
        params={"hl": "en-US"},
        headers={"x-api-key": _get_api_keys()[0]},
    )
    response.raise_for_status()
    leagues = []
    for league in response.json()["data"]["leagues"]:
        slug = league.get("slug") or ""
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
            entry: Dict[str, Any] = {
                "slug": slug,
                "name": str(league.get("name") or slug).strip(),
                "region": str(league.get("region") or ""),
            }
            image = league.get("image")
            if isinstance(image, str):
                image = (
                    image.replace("http://", "https://", 1)
                    if image.startswith("http://")
                    else image
                )
                if image.startswith("https://"):
                    entry["image"] = image
            if (league.get("displayPriority") or {}).get("status") == "hidden":
                entry["hidden"] = True
            leagues.append(entry)
    if not leagues:
        raise ValueError("[api] Empty league directory")
    return sorted(leagues, key=lambda league: (league["name"].casefold(), league["slug"]))


def api_fetch_matches(
    *,
    league_slugs: List[str],
    fetcher: Fetcher,
    config: ApiConfig,
) -> List[Match]:
    from .scrape import scrape_matches as html_fetch
    from .scrape import ScrapeConfig

    client = LolEsportsAPIClient(
        config=config,
        fetcher=fetcher,
        scrape_matches_func=lambda **kw: html_fetch(
            league_slugs=kw["league_slugs"],
            fetcher=kw["fetcher"],
            config=ScrapeConfig(tz=config.tz),
        ),
    )
    return client.fetch_matches(league_slugs=league_slugs)
