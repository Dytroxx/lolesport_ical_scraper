from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .ical import render_ical
from .models import Match
from .scrape import LEAGUE_SLUGS_DEFAULT, ScrapeConfig, scrape_matches
from .api import ApiConfig, api_fetch_matches
from .util import DiskCache, Fetcher, RateLimiter, RetryConfig
from zoneinfo import ZoneInfo

# ============================================================
# History-Storage mit SQLite (effizienter als JSON)
# ============================================================


class HistoryStore:
    """SQLite-basierte History-Speicherung für Matches.

    Deutlich effizienter als JSON: ~100x kleiner und schneller für Queries.
    """

    def __init__(self, db_path: str, retention_days: int = 365):
        self.db_path = db_path
        self.retention_days = retention_days
        self.conn: sqlite3.Connection = None  # type: ignore[assignment]
        self._init_db()

    def _init_db(self) -> None:
        """SQLite DB initialisieren mit Indexes."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_slug TEXT NOT NULL,
                league_name TEXT,
                match_id TEXT,
                match_start_utc TEXT NOT NULL,
                best_of TEXT,
                team1 TEXT NOT NULL,
                team2 TEXT NOT NULL,
                team1_code TEXT,
                team2_code TEXT,
                stage TEXT,
                match_url TEXT,
                stable_uid TEXT UNIQUE,
                state TEXT,
                team1_score INTEGER,
                team2_score INTEGER,
                winner TEXT
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_start_utc ON matches(match_start_utc)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_league_slug ON matches(league_slug)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_stable_uid ON matches(stable_uid)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_match_id ON matches(match_id)")
        self.conn.commit()

    def load(self) -> Dict[str, Any]:
        """Alle Matches aus der History laden."""
        cursor = self.conn.execute("""
            SELECT league_slug, league_name, match_id, match_start_utc, best_of,
                   team1, team2, team1_code, team2_code, stage, match_url,
                   stable_uid, state, team1_score, team2_score, winner
            FROM matches
        """)
        rows = cursor.fetchall()
        result: Dict[str, Any] = {"matches": []}
        for row in rows:
            result["matches"].append({
                "league_slug": row[0],
                "league_name": row[1],
                "match_id": row[2],
                "match_start_utc": row[3],
                "best_of": row[4],
                "team1": row[5],
                "team2": row[6],
                "team1_code": row[7],
                "team2_code": row[8],
                "stage": row[9],
                "match_url": row[10],
                "stable_uid": row[11],
                "state": row[12],
                "team1_score": row[13],
                "team2_score": row[14],
                "winner": row[15],
            })
        return result

    def save(self, matches_dicts: List[Dict[str, Any]], retention_days: int | None = None) -> None:
        """Matches speichern (upsert)."""
        cutoff_days = retention_days if retention_days is not None else self.retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days)

        self.conn.execute("BEGIN")
        try:
            # DELETE alte Einträge
            self.conn.execute(
                "DELETE FROM matches WHERE match_start_utc < ?",
                (cutoff.isoformat(),),
            )

            # UPSET neu/aktualisierte Einträge
            for m in matches_dicts:
                self.conn.execute("""
                    INSERT INTO matches (
                        league_slug, league_name, match_id, match_start_utc, best_of,
                        team1, team2, team1_code, team2_code, stage, match_url,
                        stable_uid, state, team1_score, team2_score, winner
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stable_uid) DO UPDATE SET
                        league_slug = excluded.league_slug,
                        league_name = excluded.league_name,
                        match_id = excluded.match_id,
                        match_start_utc = excluded.match_start_utc,
                        best_of = excluded.best_of,
                        team1 = excluded.team1,
                        team2 = excluded.team2,
                        team1_code = excluded.team1_code,
                        team2_code = excluded.team2_code,
                        stage = excluded.stage,
                        match_url = excluded.match_url,
                        state = excluded.state,
                        team1_score = excluded.team1_score,
                        team2_score = excluded.team2_score,
                        winner = excluded.winner
                """, (
                    m.get("league_slug"),
                    m.get("league_name"),
                    m.get("match_id"),
                    m.get("match_start_utc"),
                    m.get("best_of"),
                    m.get("team1"),
                    m.get("team2"),
                    m.get("team1_code"),
                    m.get("team2_code"),
                    m.get("stage"),
                    m.get("match_url"),
                    m.get("stable_uid"),
                    m.get("state"),
                    m.get("team1_score"),
                    m.get("team2_score"),
                    m.get("winner"),
                ))

            self.conn.commit()
            pruned = self.conn.execute(
                "SELECT COUNT(*) FROM matches WHERE match_start_utc < ?",
                (cutoff.isoformat(),),
            ).fetchone()[0]
            if pruned:
                print(f"[history] pruned {pruned} match(es) older than {cutoff_days} days")
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        if self.conn:
            self.conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *args) -> None:
        self.close()


# ============================================================
# Legacy JSON-Interoperabilität (Migration)
# ============================================================


def migrate_json_to_sqlite(json_path: Path, db_path: str) -> HistoryStore:
    """Konvertiert existierende JSON-History in SQLite."""
    if json_path.exists():
        history_data = json.loads(json_path.read_text(encoding="utf-8"))
        store = HistoryStore(db_path)
        store.save(history_data.get("matches", []))
        print(f"[migration] Converted {len(history_data.get('matches', []))} matches from JSON to SQLite")
        json_path.rename(json_path.with_suffix(".json.bak"))
        return store
    return HistoryStore(db_path)


# ============================================================
# Hauptfunktionen
# ============================================================


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
    p.add_argument("--cache-ttl", type=int, default=60 * 30, help="Cache TTL seconds (default: 1800)")
    p.add_argument("--history", default=None, help="Path to JSON file for persisting match history")
    p.add_argument(
        "--history-retention-days",
        type=int,
        default=365,
        help="Keep history entries from the last N days (default: 365)",
    )
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


def prune_history(
    matches_dicts: List[Dict[str, Any]],
    retention_days: int,
) -> List[Dict[str, Any]]:
    """Remove history entries older than *retention_days* days from now."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept: List[Dict[str, Any]] = []
    pruned = 0
    for d in matches_dicts:
        start_utc_str = d.get("match_start_utc")
        if not start_utc_str:
            continue
        try:
            start_utc = datetime.fromisoformat(start_utc_str)
            if start_utc.tzinfo is None:
                start_utc = start_utc.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Keep entries with unparseable dates so they don't vanish silently.
            kept.append(d)
            continue
        if start_utc >= cutoff:
            kept.append(d)
        else:
            pruned += 1
    if pruned:
        print(
            f"[history] pruned {pruned} match(es) older than {retention_days} days "
            f"(cutoff {cutoff.isoformat()})"
        )
    return kept


def merge_with_history(
    fresh_matches: List[Match],
    history_path: Path,
    tz_name: str,
    retention_days: int = 365,
) -> List[Match]:
    """
    Merge freshly scraped matches with historical data.

    - Fresh matches always take precedence (they may have updated scores)
    - Historical completed matches are preserved even if not in fresh data
    - History file is updated with the merged result
    - Entries older than ``retention_days`` are pruned before writing
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

    # Prune old entries before writing
    all_matches_dicts = prune_history(all_matches_dicts, retention_days)

    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps({"matches": all_matches_dicts}, indent=2), encoding="utf-8"
    )

    return list(merged_by_canonical.values())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    league_slugs = [s.strip() for s in str(args.leagues).split(",") if s.strip()]

    cache = DiskCache(Path(args.cache_dir), ttl_s=int(args.cache_ttl))
    
    # API-Config mit optimierten Werten
    api_config = ApiConfig(tz=args.tz)
    
    # Rate-Limiter und Fetcher mit API-Konfiguration synchronisieren
    rate_limiter = RateLimiter(api_config.rate_limit_s)
    retry = RetryConfig()
    fetcher = Fetcher(
        cache=cache,
        rate_limiter=rate_limiter,
        retry=retry,
        timeout_s=api_config.timeout_s,
    )

    try:
        matches = api_fetch_matches(
            league_slugs=league_slugs,
            fetcher=fetcher,
            config=api_config,
        )
    finally:
        fetcher.close()

    # Merge with history if provided
    if args.history:
        history_path = Path(args.history)

        # Auto-migrate JSON to SQLite
        db_path = str(history_path.with_suffix(".db"))
        store = migrate_json_to_sqlite(history_path.parent / "history.json", db_path)

        # Load existing history from SQLite
        history_data = store.load()
        print(f"[history] Loaded {len(history_data.get('matches', []))} matches from SQLite")

        # Merge by canonical key so the same match can't exist twice
        merged_by_canonical: Dict[Tuple[str, str, str], Match] = {}

        # Fresh matches always take precedence
        for m in matches:
            key = canonical_key_for_match(m)
            # Check if we have this match in history
            if m.match_id:
                hist_entry = next(
                    (d for d in history_data.get("matches", []) if d.get("match_id") == m.match_id),
                    None,
                )
                if hist_entry and hist_entry.get("stable_uid"):
                    matches = [with_uid(m, hist_entry["stable_uid"])]
                merged_by_canonical[key] = m
            else:
                merged_by_canonical[key] = m

        # Add historical matches that aren't in fresh data
        for hist in history_data.get("matches", []):
            key = canonical_key_for_dict(hist)
            if key not in merged_by_canonical:
                try:
                    matched_match = dict_to_match(hist, args.tz)
                    merged_by_canonical[key] = matched_match
                except Exception:
                    pass  # Skip malformed entries

        # Save back to SQLite
        all_matches_dicts = [match_to_dict(m) for m in merged_by_canonical.values()]
        all_matches_dicts.sort(key=lambda x: x.get("match_start_utc", ""))
        store.save(all_matches_dicts, retention_days=args.history_retention_days)

    ics = render_ical(matches)
    out_path = Path(args.out)
    out_path.write_text(ics, encoding="utf-8")

    leagues_found = {m.league_slug for m in matches}
    print(f"Fetched {len(matches)} matches across {len(leagues_found)} leagues; wrote {out_path}")
    return 0
