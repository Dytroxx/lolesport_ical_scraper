import json
from dataclasses import replace
from datetime import timedelta

import pytest

from lolesports_ical import main as app
from scripts.generate_feeds import generate_feeds


def run_cli(tmp_path, monkeypatch, matches, extra=()):
    monkeypatch.setattr(app, "api_fetch_matches", lambda **kwargs: matches)
    return app.main(
        [
            "--out",
            str(tmp_path / "feed.ics"),
            "--cache-dir",
            str(tmp_path / "cache"),
            *extra,
        ]
    )


def test_cli_filters_window_and_requested_leagues_without_losing_history(
    tmp_path, monkeypatch, match
):
    expired = replace(
        match,
        match_id="old",
        stable_uid="old",
        match_start_utc=match.match_start_utc - timedelta(days=400),
    )
    distant = replace(
        match,
        match_id="future",
        stable_uid="future",
        match_start_utc=match.match_start_utc + timedelta(days=60),
    )
    other = replace(match, league_slug="lck", stable_uid="other")
    db = tmp_path / "history.db"
    with app.HistoryStore(str(db)) as store:
        store.save(
            [app.match_to_dict(m) for m in [expired, match, distant, other]], retention_days=1000
        )
    assert run_cli(tmp_path, monkeypatch, [match], ["--history", str(db), "--leagues", "lec"]) == 0
    feed = (tmp_path / "feed.ics").read_text()
    assert feed.count("BEGIN:VEVENT") == 1
    with app.HistoryStore(str(db)) as store:
        history = store.load()["matches"]
    assert {m["stable_uid"] for m in history} == {match.stable_uid, "future", "other"}
    assert not db.with_name(db.name + "-wal").exists()


def test_cli_discovers_and_splits_new_league(tmp_path, monkeypatch, match):
    directory = [{"slug": "new-league", "name": "New League", "region": "EMEA"}]
    monkeypatch.setattr(app, "api_fetch_leagues", lambda fetcher: directory)
    new = replace(match, league_slug="new-league", league_name="New League")
    run_cli(tmp_path, monkeypatch, [new], ["--league-index", str(tmp_path / "leagues.json")])
    generate_feeds(tmp_path / "feed.ics", tmp_path)
    assert json.loads((tmp_path / "leagues.json").read_text()) == directory
    assert (tmp_path / "feed-new-league.ics").exists()


@pytest.mark.parametrize("existing", [True, False])
def test_league_directory_failure_keeps_previous_or_uses_matches(
    tmp_path, monkeypatch, match, existing
):
    index = tmp_path / "leagues.json"
    previous = [{"slug": "old-league", "name": "Previous League"}]
    if existing:
        index.write_text(json.dumps(previous))

    def fail(fetcher):
        raise ValueError("Directory unavailable")

    monkeypatch.setattr(app, "api_fetch_leagues", fail)
    run_cli(tmp_path, monkeypatch, [match], ["--league-index", str(index)])
    directory = json.loads(index.read_text())
    assert directory == (previous if existing else [{"slug": "lec", "name": "LEC", "region": ""}])


def test_cli_migrates_requested_json_not_unrelated_history(tmp_path, monkeypatch, match):
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"matches": [app.match_to_dict(match)]}))
    unrelated = tmp_path / "history.json"
    unrelated.write_text("unrelated")
    run_cli(tmp_path, monkeypatch, [], ["--history", str(custom)])
    assert custom.with_suffix(".db").exists()
    assert unrelated.read_text() == "unrelated"


@pytest.mark.parametrize(
    "args", [["--days", "0"], ["--history-retention-days", "-1"], ["--cache-ttl", "-1"]]
)
def test_invalid_limits_are_rejected_before_fetch(tmp_path, monkeypatch, args):
    with pytest.raises(SystemExit) as exc:
        run_cli(tmp_path, monkeypatch, [], args)
    assert exc.value.code == 2
