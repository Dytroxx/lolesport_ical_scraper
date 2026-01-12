from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .ical import render_ical
from .models import Match
from .scrape import LEAGUE_SLUGS_DEFAULT, ScrapeConfig, scrape_matches
from .util import DiskCache, Fetcher, RateLimiter, RetryConfig
from zoneinfo import ZoneInfo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lolesports_ical",
        description="Scrape LoL Esports schedules and emit an iCalendar feed",
    )
    p.add_argument("--out", default="feed.ics", help="Output .ics path (default: feed.ics)")
    p.add_argument(
        "--tz",
        default="Europe/Berlin",
        help="Local timezone for match_start_local (default: Europe/Berlin)",
    )
    p.add_argument(
        "--days", type=int, default=30, help="How many days ahead to include (default: 30)"
    )
    p.add_argument(
        "--leagues",
        default=",".join(LEAGUE_SLUGS_DEFAULT),
        help="Comma-separated league slugs (default: all supported)",
    )
    p.add_argument(
        "--cache-dir", default=str(Path(".cache") / "lolesports_ical"), help="Disk cache dir"
    )
    p.add_argument(
        "--cache-ttl", type=int, default=60 * 30, help="Cache TTL seconds (default: 1800)"
    )
    p.add_argument("--history", default=None, help="Path to JSON file for persisting match history")
    return p


def match_to_dict(m: Match) -> Dict[str, Any]:
    """Convert a Match to a JSON-serializable dict."""
    return {
        "league_slug": m.league_slug,
        "league_name": m.league_name,
        "match_id": m.match_id,
        "match_start_utc": m.match_start_utc.isoformat(),
        "best_of": m.best_of,
        "team1": m.team1,
        "team2": m.team2,
        "team1_code": m.team1_code,
        "team2_code": m.team2_code,
        "stage": m.stage,
        "match_url": m.match_url,
        "stable_uid": m.stable_uid,
        "state": m.state,
        "team1_score": m.team1_score,
        "team2_score": m.team2_score,
        "winner": m.winner,
    }


def dict_to_match(d: Dict[str, Any], tz_name: str) -> Match:
    """Convert a dict back to a Match object."""
    tz = ZoneInfo(tz_name)
    start_utc = datetime.fromisoformat(d["match_start_utc"])
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    start_local = start_utc.astimezone(tz)

    return Match(
        league_slug=d["league_slug"],
        league_name=d["league_name"],
        match_id=d.get("match_id"),
        match_start_utc=start_utc,
        match_start_local=start_local,
        best_of=d.get("best_of"),
        team1=d["team1"],
        team2=d["team2"],
        team1_code=d.get("team1_code"),
        team2_code=d.get("team2_code"),
        stage=d.get("stage"),
        match_url=d["match_url"],
        stable_uid=d["stable_uid"],
        state=d.get("state"),
        team1_score=d.get("team1_score"),
        team2_score=d.get("team2_score"),
        winner=d.get("winner"),
    )


def extract_match_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    # Common LoL Esports match URLs we emit:
    # - https://lolesports.com/live/<league>/<match_id>
    # - https://lolesports.com/match/<match_id>
    # - https://lolesports.com/matches/<match_id>
    for pat in (r"/live/[^/]+/(\d+)", r"/match/(\d+)", r"/matches/(\d+)"):
        m = __import__("re").search(pat, url)
        if m:
            return m.group(1)
    return None


def canonical_key_for_dict(d: Dict[str, Any]) -> Tuple[str, str, str]:
    league_slug = str(d.get("league_slug") or "")
    match_id = d.get("match_id") or extract_match_id_from_url(d.get("match_url"))
    if match_id:
        return ("id", league_slug, str(match_id))
    start = str(d.get("match_start_utc") or "")
    team1 = str(d.get("team1") or "").strip()
    team2 = str(d.get("team2") or "").strip()
    return ("fallback", league_slug, "|".join([start, team1, team2]))


def canonical_key_for_match(m: Match) -> Tuple[str, str, str]:
    if m.match_id:
        return ("id", m.league_slug, str(m.match_id))
    return (
        "fallback",
        m.league_slug,
        "|".join([m.match_start_utc.isoformat(), m.team1.strip(), m.team2.strip()]),
    )


def history_rank(d: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    # Higher is better.
    url = str(d.get("match_url") or "")
    state = str(d.get("state") or "")
    return (
        1 if (d.get("match_id") or extract_match_id_from_url(url)) else 0,
        1 if "/live/" in url else 0,
        1 if state == "completed" else 0,
        1 if (d.get("team1_score") is not None or d.get("team2_score") is not None) else 0,
        1 if (d.get("team1_code") or d.get("team2_code")) else 0,
    )


def with_uid(m: Match, uid: str) -> Match:
    return Match(
        league_slug=m.league_slug,
        league_name=m.league_name,
        match_id=m.match_id,
        match_start_utc=m.match_start_utc,
        match_start_local=m.match_start_local,
        best_of=m.best_of,
        team1=m.team1,
        team2=m.team2,
        team1_code=m.team1_code,
        team2_code=m.team2_code,
        stage=m.stage,
        match_url=m.match_url,
        stable_uid=uid,
        state=m.state,
        team1_score=m.team1_score,
        team2_score=m.team2_score,
        winner=m.winner,
    )


def merge_with_history(fresh_matches: List[Match], history_path: Path, tz_name: str) -> List[Match]:
    """
    Merge freshly scraped matches with historical data.

    - Fresh matches always take precedence (they may have updated scores)
    - Historical completed matches are preserved even if not in fresh data
    - History file is updated with the merged result
    """
    # Load existing history
    history_best_by_canonical: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    if history_path.exists():
        try:
            history_data = json.loads(history_path.read_text(encoding="utf-8"))
            for d in history_data.get("matches", []):
                if not isinstance(d, dict):
                    continue
                key = canonical_key_for_dict(d)
                prev = history_best_by_canonical.get(key)
                if prev is None or history_rank(d) > history_rank(prev):
                    history_best_by_canonical[key] = d
        except Exception:
            pass  # Start fresh if history is corrupted

    # Merge by canonical key so the same match can't exist twice.
    merged_by_canonical: Dict[Tuple[str, str, str], Match] = {}

    # First, add all fresh matches (but preserve previously-seen UID for the same match).
    for m in fresh_matches:
        key = canonical_key_for_match(m)
        hist = history_best_by_canonical.get(key)
        if hist and hist.get("stable_uid"):
            merged_by_canonical[key] = with_uid(m, str(hist["stable_uid"]))
        else:
            merged_by_canonical[key] = m

    # Then, add historical matches that aren't in fresh data
    for key, d in history_best_by_canonical.items():
        if key in merged_by_canonical:
            continue
        try:
            # Backfill match_id if it can be inferred from URL.
            if not d.get("match_id"):
                mid = extract_match_id_from_url(d.get("match_url"))
                if mid:
                    d = dict(d)
                    d["match_id"] = mid
            merged_by_canonical[key] = dict_to_match(d, tz_name)
        except Exception:
            pass  # Skip malformed entries

    # Save updated history
    all_matches_dicts = [match_to_dict(m) for m in merged_by_canonical.values()]
    # Sort by date for readability
    all_matches_dicts.sort(key=lambda x: x.get("match_start_utc", ""))
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps({"matches": all_matches_dicts}, indent=2), encoding="utf-8")

    return list(merged_by_canonical.values())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    league_slugs = [s.strip() for s in str(args.leagues).split(",") if s.strip()]
    config = ScrapeConfig(tz=args.tz, days=int(args.days))

    cache = DiskCache(Path(args.cache_dir), ttl_s=int(args.cache_ttl))
    fetcher = Fetcher(cache=cache, rate_limiter=RateLimiter(1.0), retry=RetryConfig())

    try:
        matches = scrape_matches(
            league_slugs=league_slugs,
            fetcher=fetcher,
            config=config,
        )
    finally:
        fetcher.close()

    # Merge with history if provided
    if args.history:
        history_path = Path(args.history)
        matches = merge_with_history(matches, history_path, tz_name=args.tz)

    ics = render_ical(matches)
    out_path = Path(args.out)
    out_path.write_text(ics, encoding="utf-8")

    leagues_found = {m.league_slug for m in matches}
    print(f"Fetched {len(matches)} matches across {len(leagues_found)} leagues; wrote {out_path}")
    return 0
