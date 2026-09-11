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
from typing import Any, Dict, List, Optional, Tuple

import httpx
from zoneinfo import ZoneInfo

from .models import Match
from .util import DiskCache, Fetcher, RateLimiter, RetryConfig, isoformat_z, stable_uid

logger = logging.getLogger(__name__)

# ============================================================
# API-Endpunkte
# ============================================================

API_BASE = "https://esports-api.lolesports.com/persisted/gw"

# Stabile API-Keys (public, aus MagicMirror-Modulen etc.)
API_KEYS = [
    "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z",
]

# Target League IDs (feste Zahlen, ändern sich nie)
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


# ============================================================
# API-Client
# ============================================================


@dataclass(frozen=True)
class ApiConfig:
    tz: str = "Europe/Berlin"
    cache_dir: str = ".cache/lolesports_ical"
    cache_ttl: int = 120  # 2 Minuten – immer frische API-Daten für aktuelle Ergebnisse
    rate_limit_s: float = 0.15  # 0.15s (~120/min), 15 Seiten × 0.15s ≈ 2s Overhead
    timeout_s: float = 10.0  # 10s – schnellere Fehlererkennung, API antwortet in <1s


def _get_api_keys() -> List[str]:
    """API-Keys aus Environment Variable laden, mit Fallback auf hardcoded Liste.
    
    Umgebungsvariable: LOL_API_KEYS (kommagetrennt)
    """
    from os import environ
    env_keys = environ.get("LOL_API_KEYS", "").strip()
    if env_keys:
        keys = [k.strip() for k in env_keys.split(",") if k.strip()]
        if keys:
            return keys
    return list(API_KEYS)


class LolEsportsAPIClient:
    """Holt Events von der unofficial LoL Esports API.

    Paginiert alle Seiten ab und parsed Events in Match-Objekte.
    Fallback auf HTML-Parser falls API nicht erreichbar.
    """

    def __init__(
        self,
        config: ApiConfig,
        fetcher: Fetcher,
        scrape_matches_func,
    ):
        self.config = config
        self.fetcher = fetcher
        self.scrape_matches_func = scrape_matches_func

    def fetch_matches(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        """Hauptmethode: API versuchen, Fallback auf HTML."""
        try:
            return self._fetch_via_api(league_slugs)
        except Exception as exc:
            print(f"[api] API fetch failed ({exc}), falling back to HTML parser")
            return self._fetch_via_html(league_slugs)

    def _fetch_via_api(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        """Paginiert alle Seiten ab und parsed Events."""
        allowed = league_slugs if league_slugs else list(LEAGUE_IDS.keys())

        all_events: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            cache_key = (
                f"api_schedule_{page_token or 'first'}"
                f"_{'_'.join(league_slugs) if league_slugs else 'all'}"
            )
            cached = self.fetcher.cache.get(cache_key)

            if cached is not None:
                events = cached.get("events", [])
                next_token = cached.get("next_token")
            else:
                url = f"{API_BASE}/getSchedule?hl=en-US"
                
                # WICHTIG: Die API erlaubt leagueIds NUR auf Seite 1!
                # Alle weiteren Seiten müssen global paginiert werden 
                # und dann client-seitig gefiltert werden.
                if page_token is None and league_slugs:
                    # Erster Request: Filtere nach Ligen
                    ids = [
                        LEAGUE_IDS[s]
                        for s in league_slugs
                        if s in LEAGUE_IDS
                    ]
                    if ids:
                        url += f"&leagueIds={','.join(ids)}"
                
                if page_token:
                    url += f"&pageToken={page_token}"

                resp = self.fetcher.get(url, headers={"x-api-key": _get_api_keys()[0]})
                
                # ========================================
                # Explizite HTTP-Status-Code Prüfung
                # ========================================
                status = resp.status_code
                
                if status == 401:
                    raise RuntimeError(
                        "[api] 401 Unauthorized – API-Key ungültig oder rotiert. "
                        "Bitte aktualisiere API_KEYS in api.py."
                    )
                if status == 403:
                    logger.warning("[api] 403 Forbidden – API-Key abgelaufen oder rotiert")
                    raise RuntimeError(
                        "[api] 403 Forbidden – API-Key abgelaufen oder rotiert. "
                        "HTML-Parser wird als Fallback verwendet."
                    )
                if status == 400:
                    error_msg = resp.text[:200]
                    logger.error(f"[api] 400 Bad Request: {error_msg}")
                    raise RuntimeError(
                        f"[api] 400 Bad Request – ungültiger Request: {error_msg}"
                    )
                if status == 404:
                    raise RuntimeError("[api] 404 Not Found – Endpunkt nicht verfügbar")
                if status == 499:
                    raise RuntimeError(
                        "[api] 499 Client Closed Request – "
                        "Client hat Verbindung abgebrochen (möglicherweise Timeout). "
                        "HTML-Parser wird als Fallback verwendet."
                    )
                if status == 429:
                    retry_after = resp.headers.get("Retry-After", "unbekannt")
                    logger.warning(f"[api] 429 Too Many Requests (Retry-After: {retry_after})")
                    # Wird vom Fetcher retryed, aber loggen für Transparenz
                    print(f"[api] 429 Too Many Requests – Retry wird durchgeführt...")
                
                if status == 500:
                    raise RuntimeError("[api] 500 Internal Server Error – API interner Fehler")
                if status == 502:
                    raise RuntimeError("[api] 502 Bad Gateway – Gateway-Fehler (Reverse Proxy)")
                if status == 503:
                    raise RuntimeError("[api] 503 Service Unavailable – API überlastet/unter Wartung")
                if status == 504:
                    raise RuntimeError("[api] 504 Gateway Timeout – Zeitüberschreitung")
                if status == 520:
                    raise RuntimeError("[api] 520 Unknown Error – Cloudflare-Serverfehler")
                if status == 521:
                    raise RuntimeError("[api] 521 Web Server Down – Origin-Server nicht erreichbar")
                if status == 522:
                    raise RuntimeError("[api] 522 Connection Timed Out – Verbindung zum Server abgelaufen")
                if status == 523:
                    raise RuntimeError("[api] 523 Origin Unreachable – Origin nicht erreichbar")
                if status == 524:
                    raise RuntimeError("[api] 524 A Timeout Occurred – Zeitüberschreitung")
                if status == 525:
                    raise RuntimeError("[api] 525 SSL Handshake Failed – SSL-Verbindung fehlgeschlagen")
                if status == 526:
                    raise RuntimeError("[api] 526 Invalid SSL Certificate – Ungültiges SSL-Zertifikat")
                if status == 527:
                    raise RuntimeError("[api] 527 Railgun Error – Railgun-Verbindungsfehler")
                if status == 528:
                    raise RuntimeError("[api] 528 Origin Connection Timed Out – Origin-Zeitüberschreitung")
                if 598 <= status <= 599:
                    raise RuntimeError(f"[api] {status} Network Read Timeout – Netzwerk-Zeitüberschreitung")
                if 500 <= status < 600:
                    raise RuntimeError(f"[api] {status} Server Error – Unbekannter Serverfehler")
                
                # 5xx-Fehler sind oben schon als RuntimeError geworfen worden
                # Hier kommen wir nur bei 2xx/3xx an
                
                if status >= 400 and status not in (429,):
                    raise RuntimeError(
                        f"[api] HTTP {status} – API-Response: {resp.text[:300]}"
                    )
                
                # JSON-Parsing prüfen
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    raise RuntimeError(
                        f"[api] JSON Decode Error – Response ist kein gültiges JSON. "
                        f"Response: {resp.text[:200]}"
                    )
                
                # API Response-Validierung
                if "data" not in data:
                    raise RuntimeError("[api] API Response: Missing 'data' key")
                if "schedule" not in data.get("data", {}):
                    raise RuntimeError("[api] API Response: Missing 'data.schedule' key")
                
                events = data["data"]["schedule"]["events"]
                pages = data["data"]["schedule"].get("pages", {})
                next_token = pages.get("older")

                self.fetcher.cache.set(
                    cache_key,
                    status=resp.status_code,
                    headers=dict(resp.headers),
                    body=json.dumps(
                        {"events": events, "next_token": next_token}
                    ).encode("utf-8"),
                )

            all_events.extend(events)

            if not next_token:
                break

            page_token = next_token

        print(f"[api] Fetched {len(all_events)} raw events from API")
        return self._parse_events(all_events, allowed)

    def _fetch_via_html(self, league_slugs: Optional[List[str]] = None) -> List[Match]:
        """Fallback: HTML-Parser."""
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
            # Nur Typ 'match' (keine Shows/Interviews)
            if event.get("type") != "match":
                continue

            match_data = event.get("match")
            if not match_data:
                continue

            teams = match_data.get("teams", [])
            if len(teams) < 2:
                continue

            # League filter
            league = event.get("league", {})
            slug = league.get("slug", "")
            if slug not in allowed:
                continue

            # Zeit
            start_str = event.get("startTime", "")
            if not start_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            # Teams
            t1 = teams[0]
            t2 = teams[1]
            team1_name = t1.get("name") or t1.get("code") or "TBD"
            team2_name = t2.get("name") or t2.get("code") or "TBD"
            team1_code = t1.get("code") or None
            team2_code = t2.get("code") or None

            # Scores
            r1 = t1.get("result") or {}
            r2 = t2.get("result") or {}
            team1_score = int(r1["gameWins"]) if "gameWins" in r1 else None
            team2_score = int(r2["gameWins"]) if "gameWins" in r2 else None

            # Winner
            winner = None
            if r1.get("outcome") == "win":
                winner = team1_name
            elif r2.get("outcome") == "win":
                winner = team2_name

            # Best of
            strategy = match_data.get("strategy") or {}
            best_of = None
            if strategy.get("type") == "bestOf":
                count = strategy.get("count")
                if count is not None:
                    best_of = f"Bo{int(count)}"

            # State
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

        # Dedup by UID
        seen: Dict[str, Match] = {}
        for m in matches:
            seen[m.stable_uid] = m
        return list(seen.values())


# ============================================================
# Standalone-Funktion für main.py
# ============================================================


def api_fetch_matches(
    *,
    league_slugs: List[str],
    fetcher: Fetcher,
    config: ApiConfig,
) -> List[Match]:
    """Hauptfunktion für den API-Call."""
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
