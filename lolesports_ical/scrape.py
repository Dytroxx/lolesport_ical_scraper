from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
import re

from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from .models import Match
from .util import Fetcher, isoformat_z, stable_uid


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


class HtmlScraper:
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
                if "/match/" in href or "/matches/" in href or "/live/" in href:
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
                    team1_code=None,  # HTML fallback doesn't have codes
                    team2_code=None,
                    stage=stage,
                    match_url=match_url,
                    stable_uid=uid,
                    state=None,
                    team1_score=None,
                    team2_score=None,
                    winner=None,
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

                # Teams and scores
                teams = ev.get("matchTeams") or []
                t1_data = teams[0] if len(teams) >= 1 else {}
                t2_data = teams[1] if len(teams) >= 2 else {}
                team1 = str(t1_data.get("name") or t1_data.get("code") or "TBD")
                team2 = str(t2_data.get("name") or t2_data.get("code") or "TBD")
                team1_code = t1_data.get("code") or None
                team2_code = t2_data.get("code") or None
                
                # Extract scores from team result
                team1_score = None
                team2_score = None
                t1_result = t1_data.get("result") or {}
                t2_result = t2_data.get("result") or {}
                if t1_result.get("gameWins") is not None:
                    team1_score = int(t1_result.get("gameWins", 0))
                if t2_result.get("gameWins") is not None:
                    team2_score = int(t2_result.get("gameWins", 0))

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

                # Match state and winner
                state = ev.get("state") or match.get("state") or None
                winner = None
                if state == "completed":
                    if t1_result.get("outcome") == "win":
                        winner = team1
                    elif t2_result.get("outcome") == "win":
                        winner = team2
                    elif team1_score is not None and team2_score is not None:
                        if team1_score > team2_score:
                            winner = team1
                        elif team2_score > team1_score:
                            winner = team2

                stage = ev.get("blockName") or None
                
                # Build proper match URL using match ID
                match_id = match.get("id") or ev.get("id")
                if match_id:
                    match_url = f"https://lolesports.com/live/{slug}/{match_id}"
                else:
                    match_url = f"https://lolesports.com/schedule?leagues={slug}"

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
                        team1_code=team1_code,
                        team2_code=team2_code,
                        stage=str(stage) if stage else None,
                        match_url=match_url,
                        stable_uid=uid,
                        state=state,
                        team1_score=team1_score,
                        team2_score=team2_score,
                        winner=winner,
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


def scrape_matches(
    *,
    league_slugs: List[str],
    fetcher: Fetcher,
    config: ScrapeConfig,
) -> List[Match]:
    return HtmlScraper(fetcher).fetch_matches(league_slugs, config=config)
