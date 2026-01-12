from __future__ import annotations

import argparse
from pathlib import Path

from .ical import render_ical
from .scrape import LEAGUE_SLUGS_DEFAULT, ScrapeConfig, scrape_matches
from .util import DiskCache, Fetcher, RateLimiter, RetryConfig, env_api_key


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lolesports_ical", description="Scrape LoL Esports schedules and emit an iCalendar feed")
    p.add_argument("--out", default="feed.ics", help="Output .ics path (default: feed.ics)")
    p.add_argument("--tz", default="Europe/Berlin", help="Local timezone for match_start_local (default: Europe/Berlin)")
    p.add_argument("--days", type=int, default=30, help="How many days ahead to include (default: 30)")
    p.add_argument(
        "--leagues",
        default=",".join(LEAGUE_SLUGS_DEFAULT),
        help="Comma-separated league slugs (default: all supported)",
    )
    p.add_argument("--no-api", action="store_true", help="Disable API mode and force HTML parsing")
    p.add_argument("--cache-dir", default=str(Path(".cache") / "lolesports_ical"), help="Disk cache dir")
    p.add_argument("--cache-ttl", type=int, default=60 * 30, help="Cache TTL seconds (default: 1800)")
    return p


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
            prefer_api=not bool(args.no_api),
            api_key=env_api_key(),
        )
    finally:
        fetcher.close()

    ics = render_ical(matches)
    out_path = Path(args.out)
    out_path.write_text(ics, encoding="utf-8")

    leagues_found = {m.league_slug for m in matches}
    print(f"Fetched {len(matches)} matches across {len(leagues_found)} leagues; wrote {out_path}")
    return 0
