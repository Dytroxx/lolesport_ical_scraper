import json
from dataclasses import replace

import pytest

from lolesports_ical.ical import render_ical
from scripts.generate_feeds import generate_feeds


def test_dynamic_split_preserves_events_and_writes_empty_league(tmp_path, match):
    source = tmp_path / "feed.ics"
    new = replace(match, league_slug="new-league", league_name="Neue Liga " + "é" * 90)
    source.write_bytes(render_ical([new]).encode("utf-8"))
    (tmp_path / "leagues.json").write_text(
        json.dumps(
            [
                {"slug": "new-league", "name": new.league_name, "region": "EMEA"},
                {"slug": "offseason", "name": "Offseason", "region": ""},
            ]
        ),
        encoding="utf-8",
    )

    generate_feeds(source, tmp_path)

    raw = (tmp_path / "feed-new-league.ics").read_bytes()
    assert raw.count(b"BEGIN:VEVENT") == 1
    assert b"\r\n\r\n" not in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert b"END:VEVENT\r\nEND:VCALENDAR\r\n" in raw
    assert all(len(line) <= 75 for line in raw.split(b"\r\n"))
    assert "BEGIN:VEVENT" not in (tmp_path / "feed-offseason.ics").read_text()
    directory = json.loads((tmp_path / "leagues.json").read_text())
    assert {league["slug"] for league in directory} == {"new-league", "offseason"}


def test_split_multiple_events_do_not_run_together(tmp_path, match):
    source = tmp_path / "feed.ics"
    source.write_bytes(
        render_ical([match, replace(match, match_id="456", stable_uid="second")]).encode()
    )
    generate_feeds(source, tmp_path)
    raw = (tmp_path / "feed-lec.ics").read_bytes()
    assert b"END:VEVENT\r\nBEGIN:VEVENT" in raw
    assert raw.count(b"BEGIN:VEVENT") == 2


def test_legacy_feed_without_explicit_slug(tmp_path, match):
    source = tmp_path / "feed.ics"
    source.write_text(render_ical([match]).replace("X-LOL-LEAGUE-SLUG:lec\r\n", ""))
    generate_feeds(source, tmp_path)
    assert (tmp_path / "feed-lec.ics").exists()


@pytest.mark.parametrize("slug", ["../escape", "", "bad/slug"])
def test_rejects_unsafe_event_slugs(tmp_path, match, slug):
    source = tmp_path / "feed.ics"
    source.write_bytes(
        render_ical([match]).replace("X-LOL-LEAGUE-SLUG:lec", f"X-LOL-LEAGUE-SLUG:{slug}").encode()
    )
    if not slug:
        source.write_bytes(source.read_bytes().replace(b"League: LEC", b"Missing"))
    with pytest.raises(ValueError, match="league slug"):
        generate_feeds(source, tmp_path)


def test_unterminated_event_is_an_error(tmp_path):
    source = tmp_path / "feed.ics"
    source.write_text("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:broken\n")
    with pytest.raises(ValueError, match="Unterminated"):
        generate_feeds(source, tmp_path)
