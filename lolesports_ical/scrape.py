from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
import re

from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from .models import Match
from .util import Fetcher, isoformat_z, stable_uid, try_extract_api_key_from_text


LEAGUE_SLUGS_DEFAULT = [
    "emea_masters",
    "first_stand",
    "lck",
    "lcs",
    "lec",
    "lpl",
    "msi",
    "worlds",
]


@dataclass(frozen=True)
class ScrapeConfig:
    tz: str = "Europe/Berlin"
    days: int = 30
    locale: str = "en-US"


class BaseScraper:
    def fetch_matches(self, league_slugs: List[str], *, config: ScrapeConfig) -> List[Match]:
        raise NotImplementedError


class ApiScraper(BaseScraper):
    """Best-effort support for LoL Esports internal schedule endpoints.

    This typically uses the `esports-api.lolesports.com/persisted/gw/*` endpoints.
    Some deployments require an `x-api-key` header.
    """

    API_BASE = "https://esports-api.lolesports.com"

    def __init__(self, fetcher: Fetcher, *, api_key: Optional[str] = None) -> None:
        self.fetcher = fetcher
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _get_json(self, path: str, *, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.API_BASE}{path}"
        resp = self.fetcher.get(url, params=params, headers=self._headers())
        return resp.json()

    def _league_slug_to_id_map(self, *, locale: str) -> Dict[str, str]:
        # Known endpoint name, but structure can change; keep parser permissive.
        data = self._get_json("/persisted/gw/getLeagues", params={"hl": locale})
        leagues = (
            data.get("data", {}).get("leagues")
            or data.get("leagues")
            or []
        )
        mapping: Dict[str, str] = {}
        for lg in leagues:
            slug = lg.get("slug") or lg.get("leagueSlug")
            league_id = lg.get("id") or lg.get("leagueId")
            if slug and league_id:
                mapping[str(slug)] = str(league_id)
        return mapping

    def _parse_events_to_matches(
        self,
        events: Iterable[Dict[str, Any]],
        *,
        league_slug: str,
        tz: ZoneInfo,
        league_page_url: str,
    ) -> List[Match]:
        out: List[Match] = []
        for ev in events:
            start = ev.get("startTime") or ev.get("start_time")
            if not start:
                continue
            try:
                start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            except Exception:
                continue
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            start_utc = start_dt.astimezone(timezone.utc)
            start_local = start_utc.astimezone(tz)

            league = ev.get("league") or {}
            league_name = league.get("name") or league.get("displayName") or league_slug

            match = ev.get("match") or {}
            strategy = match.get("strategy") or {}
            best_of_val = strategy.get("count") or match.get("bestOf")
            best_of = None
            if best_of_val:
                try:
                    best_of = f"Bo{int(best_of_val)}"
                except Exception:
                    best_of = str(best_of_val)

            teams = match.get("teams") or []
            t1 = "TBD"
            t2 = "TBD"
            if len(teams) >= 1:
                t1 = (teams[0].get("name") or teams[0].get("code") or "TBD")
            if len(teams) >= 2:
                t2 = (teams[1].get("name") or teams[1].get("code") or "TBD")

            stage = (
                (ev.get("blockName") or ev.get("stage") or ev.get("tournamentStage"))
                or None
            )

            match_url = match.get("matchUrl") or ev.get("matchUrl") or league_page_url

            uid = stable_uid(
                league_slug=league_slug,
                match_start_utc_iso=isoformat_z(start_utc),
                team1=t1,
                team2=t2,
                stage=stage,
                match_url=match_url,
            )

            out.append(
                Match(
                    league_slug=league_slug,
                    league_name=str(league_name),
                    match_start_utc=start_utc,
                    match_start_local=start_local,
                    best_of=best_of,
                    team1=str(t1),
                    team2=str(t2),
                    stage=str(stage) if stage else None,
                    match_url=str(match_url),
                    stable_uid=uid,
                )
            )
        return out

    def fetch_matches(self, league_slugs: List[str], *, config: ScrapeConfig) -> List[Match]:
        tz = ZoneInfo(config.tz)
        league_ids = self._league_slug_to_id_map(locale=config.locale)

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=2)  # include recent
        end_time = now + timedelta(days=config.days)

        all_matches: List[Match] = []
        for slug in league_slugs:
            league_page_url = f"https://lolesports.com/{config.locale}/leagues/{slug}"
            league_id = league_ids.get(slug)
            if not league_id:
                continue

            # Common schedule endpoint; fields/params can change.
            data = self._get_json(
                "/persisted/gw/getSchedule",
                params={
                    "hl": config.locale,
                    "leagueId": league_id,
                },
            )
            sched = data.get("data", {}).get("schedule") or data.get("schedule") or {}
            events = sched.get("events") or []

            matches = self._parse_events_to_matches(events, league_slug=slug, tz=tz, league_page_url=league_page_url)

            # Filter time window.
            matches = [m for m in matches if start_time <= m.match_start_utc <= end_time]
            all_matches.extend(matches)

        return all_matches


class HtmlScraper(BaseScraper):
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    @staticmethod
    def parse_schedule_html(
        html: str,
        *,
        league_slugs: List[str],
        tz_name: str,
        page_url: str,
    ) -> List[Match]:
        tz = ZoneInfo(tz_name)
        soup = BeautifulSoup(html, "lxml")

        # 1) Prefer structured SSR data when available.
        apollo_matches = HtmlScraper._parse_from_apollo_ssr(html, league_slugs=league_slugs, tz=tz, page_url=page_url)
        if apollo_matches:
            return apollo_matches

        # Heuristic: Next.js often embeds JSON in a script tag.
        league_name_by_slug: Dict[str, str] = {s: s for s in league_slugs}
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                # Best-effort: look for league display names.
                text = next_data.string
                for slug in league_slugs:
                    # try to find "slug":"...","name":"..."
                    # fall back to slug
                    pass
            except Exception:
                pass

        matches: List[Match] = []

        # Find candidate match containers by presence of a <time datetime> and two team labels.
        for time_el in soup.find_all("time"):
            dt_raw = time_el.get("datetime") or time_el.get("dateTime")
            if not dt_raw:
                continue
            try:
                start_dt = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
            except Exception:
                continue
            if start_dt.tzinfo is None:
                # treat as UTC if machine-readable but missing tz
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            start_utc = start_dt.astimezone(timezone.utc)
            start_local = start_utc.astimezone(tz)

            # Walk up to a container that actually represents a match card.
            container = None
            cur = time_el
            for _ in range(12):
                if cur is None:
                    break
                if getattr(cur, "name", None) in ("article", "div", "li", "section"):
                    # Heuristic: must contain a league link for one of our slugs.
                    has_league_link = False
                    for a in cur.find_all("a", href=True):
                        href = str(a.get("href") or "")
                        if any(f"/leagues/{s}" in href for s in league_slugs):
                            has_league_link = True
                            break
                    if has_league_link:
                        # Must also contain teams marker.
                        if cur.select(".team") or (" vs " in cur.get_text(" ", strip=True).lower()):
                            container = cur
                            break
                cur = cur.parent

            if container is None:
                continue

            text = container.get_text(" ", strip=True)
            if not text:
                continue

            # Try infer league slug + name from any /leagues/<slug> link in the container.
            slug = None
            league_name = None
            for a in container.find_all("a", href=True):
                href = str(a.get("href") or "")
                for s in league_slugs:
                    if f"/leagues/{s}" in href:
                        slug = s
                        txt = a.get_text(" ", strip=True)
                        league_name = txt or None
                        break
                if slug:
                    break
            if not slug:
                # If we can't assign it, skip (caller needs per-league output).
                continue

            league_name = league_name or league_name_by_slug.get(slug, slug)

            # Team names: prefer explicit team elements.
            team1 = "TBD"
            team2 = "TBD"

            team_els = container.select(".teams .team, .team")
            team_texts = [e.get_text(" ", strip=True) for e in team_els]
            team_texts = [t for t in team_texts if t]
            if len(team_texts) >= 2:
                team1, team2 = team_texts[0], team_texts[1]
            else:
                # Fallback: pick tokens around a 'vs' marker.
                tokens = [t for t in container.get_text("\n", strip=True).split("\n") if t]
                vs_idx = None
                for i, t in enumerate(tokens):
                    if t.strip().lower() in {"vs", "v"}:
                        vs_idx = i
                        break
                if vs_idx is not None:
                    if vs_idx - 1 >= 0:
                        team1 = tokens[vs_idx - 1].strip() or "TBD"
                    if vs_idx + 1 < len(tokens):
                        team2 = tokens[vs_idx + 1].strip() or "TBD"

            # Stage / best-of (best-effort)
            stage = None
            best_of = None
            if "Bo5" in text:
                best_of = "Bo5"
            elif "Bo3" in text:
                best_of = "Bo3"
            elif "Bo1" in text:
                best_of = "Bo1"

            # Attempt stage from labeled chips
            for chip in container.find_all(["span", "div"]):
                t = chip.get_text(" ", strip=True)
                if t and t.lower() in {"playoffs", "swiss", "groups", "group stage", "final", "semifinal", "quarterfinal"}:
                    stage = t
                    break

            match_url = page_url
            for a in container.find_all("a", href=True):
                href = str(a.get("href") or "")
                if "/match/" in href or "/matches/" in href:
                    match_url = href if href.startswith("http") else f"https://lolesports.com{href}"
                    break

            uid = stable_uid(
                league_slug=slug,
                match_start_utc_iso=isoformat_z(start_utc),
                team1=team1,
                team2=team2,
                stage=stage,
                match_url=match_url,
            )

            matches.append(
                Match(
                    league_slug=slug,
                    league_name=league_name,
                    match_start_utc=start_utc,
                    match_start_local=start_local,
                    best_of=best_of,
                    team1=team1,
                    team2=team2,
                    stage=stage,
                    match_url=match_url,
                    stable_uid=uid,
                )
            )

        # Deduplicate by UID
        uniq: Dict[str, Match] = {}
        for m in matches:
            uniq[m.stable_uid] = m
        return list(uniq.values())

    @staticmethod
    def _parse_from_apollo_ssr(
        html: str,
        *,
        league_slugs: List[str],
        tz: ZoneInfo,
        page_url: str,
    ) -> List[Match]:
        """Parse server-rendered Apollo cache payload embedded in the schedule page.

        LoL Esports schedule pages often include a script like:
        `(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push({...})`

        The object is almost-JSON but may contain `undefined`; we normalize and decode.
        """

        payload_texts: List[str] = []
        # Extract the argument to `.push(<here>)` from any script containing ApolloSSRDataTransport.
        for m in re.finditer(r"ApolloSSRDataTransport[\s\S]{0,2000}?\.push\(", html):
            start = m.end()
            script_end = html.find("</script>", start)
            if script_end == -1:
                continue
            end_paren = html.rfind(")", start, script_end)
            if end_paren == -1:
                continue
            payload_texts.append(html[start:end_paren])

        def normalize_js_object(text: str) -> str:
            # Replace bare `undefined` tokens with null (not inside quotes).
            return re.sub(r"\bundefined\b", "null", text)

        def find_event_matches(obj: Any) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            if isinstance(obj, dict):
                if obj.get("__typename") == "EventMatch":
                    out.append(obj)
                for v in obj.values():
                    out.extend(find_event_matches(v))
            elif isinstance(obj, list):
                for it in obj:
                    out.extend(find_event_matches(it))
            return out

        matches: List[Match] = []
        for text in payload_texts:
            try:
                payload = json.loads(normalize_js_object(text))
            except Exception:
                continue

            events = find_event_matches(payload)
            for ev in events:
                league = ev.get("league") or {}
                slug = league.get("slug")
                if not slug or slug not in set(league_slugs):
                    continue

                start = ev.get("startTime")
                if not start:
                    continue
                try:
                    start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                except Exception:
                    continue
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                start_utc = start_dt.astimezone(timezone.utc)
                start_local = start_utc.astimezone(tz)

                league_name = league.get("name") or slug

                # Teams
                teams = ev.get("matchTeams") or []
                t1 = teams[0].get("name") if len(teams) >= 1 else None
                t2 = teams[1].get("name") if len(teams) >= 2 else None
                team1 = str(t1) if t1 else "TBD"
                team2 = str(t2) if t2 else "TBD"

                # Best-of
                match = ev.get("match") or {}
                strategy = match.get("strategy") or {}
                best_of = None
                cnt = strategy.get("count")
                if cnt is not None:
                    try:
                        best_of = f"Bo{int(cnt)}"
                    except Exception:
                        best_of = str(cnt)

                stage = ev.get("blockName") or None
                match_url = page_url

                uid = stable_uid(
                    league_slug=str(slug),
                    match_start_utc_iso=isoformat_z(start_utc),
                    team1=team1,
                    team2=team2,
                    stage=str(stage) if stage else None,
                    match_url=match_url,
                )

                matches.append(
                    Match(
                        league_slug=str(slug),
                        league_name=str(league_name),
                        match_start_utc=start_utc,
                        match_start_local=start_local,
                        best_of=best_of,
                        team1=team1,
                        team2=team2,
                        stage=str(stage) if stage else None,
                        match_url=match_url,
                        stable_uid=uid,
                    )
                )

        uniq: Dict[str, Match] = {}
        for m in matches:
            uniq[m.stable_uid] = m
        return list(uniq.values())

    def fetch_matches(self, league_slugs: List[str], *, config: ScrapeConfig) -> List[Match]:
        page_url = f"https://lolesports.com/schedule?leagues={','.join(league_slugs)}"
        resp = self.fetcher.get(page_url)
        return self.parse_schedule_html(resp.text, league_slugs=league_slugs, tz_name=config.tz, page_url=page_url)


def discover_api_key_via_html(fetcher: Fetcher) -> Optional[str]:
    # Best-effort: fetch schedule page and scan for api key patterns.
    url = "https://lolesports.com/schedule"
    resp = fetcher.get(url)
    key = try_extract_api_key_from_text(resp.text)
    if key:
        return key

    # Next.js script sources often include the key; try a small subset.
    soup = BeautifulSoup(resp.text, "lxml")
    scripts = [str(s.get("src")) for s in soup.find_all("script") if s.get("src")]
    for src in scripts[:5]:
        src_url = src if src.startswith("http") else f"https://lolesports.com{src}"
        try:
            js = fetcher.get(src_url).text
        except Exception:
            continue
        key = try_extract_api_key_from_text(js)
        if key:
            return key

    return None


def scrape_matches(
    *,
    league_slugs: List[str],
    fetcher: Fetcher,
    config: ScrapeConfig,
    prefer_api: bool = True,
    api_key: Optional[str] = None,
) -> List[Match]:
    if prefer_api:
        key = api_key or discover_api_key_via_html(fetcher)
        if key:
            try:
                return ApiScraper(fetcher, api_key=key).fetch_matches(league_slugs, config=config)
            except Exception:
                # Fall back to HTML if API fails
                pass
    return HtmlScraper(fetcher).fetch_matches(league_slugs, config=config)
