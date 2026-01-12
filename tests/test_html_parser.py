from __future__ import annotations

from pathlib import Path

from lolesports_ical.scrape import HtmlScraper


def test_parse_schedule_html_fixture() -> None:
    html = Path(__file__).parent / "fixtures" / "schedule_fixture.html"
    text = html.read_text(encoding="utf-8")

    matches = HtmlScraper.parse_schedule_html(
        text,
        league_slugs=["lec"],
        tz_name="Europe/Berlin",
        page_url="https://lolesports.com/schedule?leagues=lec",
    )

    assert len(matches) == 1
    m = matches[0]
    assert m.league_slug == "lec"
    assert m.team1 == "G2 Esports"
    assert m.team2 == "Fnatic"
    assert m.best_of == "Bo3"
    assert m.stage == "Playoffs"
    assert m.match_url.startswith("https://lolesports.com/")
    assert m.stable_uid.endswith("@lolesports")


def test_parse_schedule_html_apollo_fixture() -> None:
    html = Path(__file__).parent / "fixtures" / "schedule_apollo_fixture.html"
    text = html.read_text(encoding="utf-8")

    matches = HtmlScraper.parse_schedule_html(
        text,
        league_slugs=["lec"],
        tz_name="Europe/Berlin",
        page_url="https://lolesports.com/schedule?leagues=lec",
    )

    assert len(matches) == 1
    m = matches[0]
    assert m.league_slug == "lec"
    assert m.league_name == "LEC"
    assert m.best_of == "Bo3"
    assert m.team1 == "Team One"
    assert m.team2 == "Team Two"
