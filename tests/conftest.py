from datetime import datetime, timezone

import pytest

from lolesports_ical.models import Match


@pytest.fixture
def match() -> Match:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    return Match(
        league_slug="lec",
        league_name="LEC",
        match_id="123",
        match_start_utc=start,
        match_start_local=start,
        best_of="Bo3",
        team1="Team A",
        team2="Team B",
        team1_code="A",
        team2_code="B",
        stage="Final",
        match_url="https://lolesports.com/live/lec/123",
        stable_uid="original@lolesports",
        state="unstarted",
    )
