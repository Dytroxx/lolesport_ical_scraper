"""Tests for feed comparison logic used in the GitHub Actions workflow.

The workflow compares feeds by stripping DTSTAMP lines before diff,
since DTSTAMP changes on every generation but doesn't represent actual data changes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from lolesports_ical.ical import render_ical
from lolesports_ical.models import Match


def normalize_feed_for_comparison(feed: str) -> str:
    """
    Normalize an iCal feed for comparison by removing DTSTAMP lines.
    
    This mirrors the workflow logic:
    grep -v '^DTSTAMP:' feed.ics
    """
    lines = feed.splitlines()
    return "\n".join(line for line in lines if not line.startswith("DTSTAMP:"))


def create_test_match(
    match_id: str = "123",
    team1: str = "Team A",
    team2: str = "Team B",
    state: str = "unstarted",
    team1_score: int | None = None,
    team2_score: int | None = None,
    winner: str | None = None,
) -> Match:
    """Create a test match with sensible defaults."""
    from zoneinfo import ZoneInfo
    
    start_utc = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    start_local = start_utc.astimezone(ZoneInfo("Europe/Berlin"))
    
    return Match(
        league_slug="lec",
        league_name="LEC",
        match_id=match_id,
        match_start_utc=start_utc,
        match_start_local=start_local,
        best_of="Bo3",
        team1=team1,
        team2=team2,
        team1_code="TA",
        team2_code="TB",
        stage="Playoffs",
        match_url=f"https://lolesports.com/live/lec/{match_id}",
        stable_uid=f"test-uid-{match_id}@lolesports",
        state=state,
        team1_score=team1_score,
        team2_score=team2_score,
        winner=winner,
    )


def test_dtstamp_is_present_in_feed() -> None:
    """Verify that DTSTAMP is actually included in generated feeds."""
    match = create_test_match()
    feed = render_ical([match])
    assert "DTSTAMP:" in feed


def test_dtstamp_stripped_in_normalization() -> None:
    """Verify that normalization removes DTSTAMP lines."""
    match = create_test_match()
    feed = render_ical([match])
    normalized = normalize_feed_for_comparison(feed)
    assert "DTSTAMP:" not in normalized


def test_identical_matches_produce_equal_normalized_feeds() -> None:
    """Two feeds with the same match data should be equal after normalization."""
    match = create_test_match()
    
    # Generate two feeds (DTSTAMP will differ if there's any time gap)
    feed1 = render_ical([match])
    feed2 = render_ical([match])
    
    # Normalize both
    normalized1 = normalize_feed_for_comparison(feed1)
    normalized2 = normalize_feed_for_comparison(feed2)
    
    assert normalized1 == normalized2


def test_different_scores_produce_different_normalized_feeds() -> None:
    """When match scores change, normalized feeds should differ."""
    match_unfinished = create_test_match(state="unstarted")
    match_finished = create_test_match(
        state="completed",
        team1_score=2,
        team2_score=1,
        winner="Team A",
    )
    
    feed1 = render_ical([match_unfinished])
    feed2 = render_ical([match_finished])
    
    normalized1 = normalize_feed_for_comparison(feed1)
    normalized2 = normalize_feed_for_comparison(feed2)
    
    assert normalized1 != normalized2


def test_new_match_produces_different_normalized_feed() -> None:
    """Adding a new match should produce a different normalized feed."""
    match1 = create_test_match(match_id="123")
    match2 = create_test_match(match_id="456", team1="Team C", team2="Team D")
    
    feed_one_match = render_ical([match1])
    feed_two_matches = render_ical([match1, match2])
    
    normalized1 = normalize_feed_for_comparison(feed_one_match)
    normalized2 = normalize_feed_for_comparison(feed_two_matches)
    
    assert normalized1 != normalized2


def test_match_time_change_produces_different_normalized_feed() -> None:
    """Rescheduled match should produce a different normalized feed."""
    from zoneinfo import ZoneInfo
    
    match1 = create_test_match()
    
    start_utc = datetime(2026, 1, 16, 18, 0, tzinfo=timezone.utc)  # Different day
    start_local = start_utc.astimezone(ZoneInfo("Europe/Berlin"))
    
    match2 = Match(
        league_slug="lec",
        league_name="LEC",
        match_id="123",
        match_start_utc=start_utc,
        match_start_local=start_local,
        best_of="Bo3",
        team1="Team A",
        team2="Team B",
        team1_code="TA",
        team2_code="TB",
        stage="Playoffs",
        match_url="https://lolesports.com/live/lec/123",
        stable_uid="test-uid-123@lolesports",
        state="unstarted",
        team1_score=None,
        team2_score=None,
        winner=None,
    )
    
    feed1 = render_ical([match1])
    feed2 = render_ical([match2])
    
    normalized1 = normalize_feed_for_comparison(feed1)
    normalized2 = normalize_feed_for_comparison(feed2)
    
    assert normalized1 != normalized2


def test_normalization_preserves_all_other_fields() -> None:
    """Normalization should only remove DTSTAMP, keeping all other data intact."""
    match = create_test_match(
        state="completed",
        team1_score=2,
        team2_score=0,
        winner="Team A",
    )
    feed = render_ical([match])
    normalized = normalize_feed_for_comparison(feed)
    
    # These should all be preserved
    assert "BEGIN:VCALENDAR" in normalized
    assert "END:VCALENDAR" in normalized
    assert "BEGIN:VEVENT" in normalized
    assert "END:VEVENT" in normalized
    assert "UID:" in normalized
    assert "DTSTART:" in normalized
    assert "DTEND:" in normalized
    assert "SUMMARY:" in normalized
    assert "DESCRIPTION:" in normalized
    assert "URL:" in normalized


def test_empty_feed_normalization() -> None:
    """Empty match list should produce a valid calendar that normalizes correctly."""
    feed = render_ical([])
    normalized = normalize_feed_for_comparison(feed)
    
    assert "BEGIN:VCALENDAR" in normalized
    assert "END:VCALENDAR" in normalized
    assert "BEGIN:VEVENT" not in normalized
