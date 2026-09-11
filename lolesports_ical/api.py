"""Unoffizielle LoL Esports API für Schedules.

Hauptquelle: https://esports-api.lolesports.com/persisted/gw
Falls die API nicht erreichbar ist, wird auf den HTML-Parser (scrape.py)
zurückgegriffen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from zoneinfo import ZoneInfo

from .models import Match
from .util import DiskCache, Fetcher, RateLimiter, RetryConfig, isoformat_z, stable_uid

logger = logging.getLogger(__name__)

API_BASE = "https://esports-api.lolesports.com/persisted/gw"

API_KEYS = [
    "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z",
]

LEAGUE_IDS = {
    "lec": "98767991302996019",
    "lck": "98767991310872058",
    "lcs": "98767991299243165",
    "lpl": "98767991314006698",
    "msi": "98767991325878492",
    "worlds": "98767975604431411",
    "emea_masters": "100695891328981122",
    "first_stand": "113464388705111224",
}


@dataclass(frozen=True)
class ApiConfig:
    tz: str = "Europe/Berlin"
    cache_dir: str = ".cache/lolesports_ical"
    cache_ttl: int = 120
    rate_limit_s: float = 0.15
    timeout_s: float = 10.0


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
        """Holt zukünftige Events (unstarted, inProgress) über die API.
        
        Die API hat bidirektionale Pagination:
        - older: vergangene Events (completed)
        - newer: zukünftige Events (unstarted, inProgress)
        
        Wir folgen NUR der 'newer'-Richtung, weil die alle zukünftigen Events enthält.
        """
        allowed = league_slugs if league_slugs else list(LEAGUE_IDS.keys())
        all_events: List[Dict[str, Any]] = []

        # Hole nur die "newer"-Richtung (zukünftige Events)
        page_token: Optional[str] = None
        while True:
            url = f"{API_BASE}/getSchedule?hl=en-US"
            if page_token:
                url += f"&pageToken={page_token}"

            # Cache prüfen
            cache_key = f"api_newer_{page_token or 'first'}"
            cached = self.fetcher.cache.get(cache_key)
            
            if cached is not None:
                # DiskCache speichert Roh-Payload mit body_b64 — erst decodieren und parsen
                body_raw = str(cached.get("body_b64", "")).encode("latin1")
                payload = json.loads(body_raw)
                events = payload.get("events", [])
                next_token = payload.get("next_token")
            else:
                resp = self.fetcher.get(url, headers={"x-api-key": _get_api_keys()[0]})
                data = _validate_response(resp)

                events = data["data"]["schedule"]["events"]
                pages = data["data"]["schedule"].get("pages", {})
                next_token = pages.get("newer")

                self.fetcher.cache.set(
                    cache_key,
                    status=resp.status_code,
                    headers=dict(resp.headers),
                    body=json.dumps({
                        "events": events,
                        "next_token": next_token,
                    }).encode("utf-8"),
                )

            all_events.extend(events)

            if not next_token:
                break
            page_token = next_token

        print(f"[api] Fetched {len(all_events)} raw events from API")
        
        # Wenn die API 0 Events zurückgibt, versuche den HTML-Fallback
        if not all_events:
            logger.warning("[api] API returned 0 events, falling back to HTML parser")
            return self._fetch_via_html(allowed)
        
        return self._parse_events(all_events, allowed)

    def _fetch_via_html(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        from .scrape import scrape_matches as html_fetch
        from .scrape import ScrapeConfig

        return html_fetch(
            league_slugs=league_slugs or list(LEAGUE_IDS.keys()),
            fetcher=self.fetcher,
            config=ScrapeConfig(tz=self.config.tz),
        )

    def _parse_events(
        self, events: List[Dict[str, Any]], allowed: List[str]
    ) -> List[Match]:
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
            slug = league.get("slug", "")
            if slug not in allowed:
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
            team1_score = int(r1["gameWins"]) if "gameWins" in r1 else None
            team2_score = int(r2["gameWins"]) if "gameWins" in r2 else None

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
