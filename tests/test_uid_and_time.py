from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from lolesports_ical.util import isoformat_z, stable_uid


def test_uid_stable() -> None:
    dt = datetime(2026, 1, 12, 18, 0, tzinfo=timezone.utc)
    uid1 = stable_uid(
        league_slug="lec",
        match_start_utc_iso=isoformat_z(dt),
        team1="Team A",
        team2="Team B",
        stage="Playoffs",
        match_url="https://lolesports.com/match/123",
    )
    uid2 = stable_uid(
        league_slug="lec",
        match_start_utc_iso=isoformat_z(dt),
        team1="Team A",
        team2="Team B",
        stage="Playoffs",
        match_url="https://lolesports.com/match/123",
    )
    assert uid1 == uid2
    assert uid1.endswith("@lolesports")
    assert len(uid1.split("@")[0]) == 32


def test_timezone_conversion_berlin() -> None:
    utc_dt = datetime(2026, 1, 12, 18, 0, tzinfo=timezone.utc)
    berlin = ZoneInfo("Europe/Berlin")
    local = utc_dt.astimezone(berlin)
    assert local.tzinfo is not None
    # In winter, Berlin is UTC+1
    assert local.hour == 19
