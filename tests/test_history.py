import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from lolesports_ical.main import (
    HistoryStore,
    canonical_key_for_dict,
    canonical_key_for_match,
    dict_to_match,
    match_to_dict,
    merge_matches,
    migrate_json_to_sqlite,
)


@pytest.mark.parametrize("fresh_score", [None, 0])
def test_preserves_completed_result_and_uid(match, fresh_score):
    old = replace(match, state="completed", team1_score=2, team2_score=1, winner="Team A")
    fresh = replace(match, team1_score=fresh_score, team2_score=fresh_score, stable_uid="changed")
    merged = merge_matches([fresh], [match_to_dict(old)], "Europe/Berlin")
    assert merged == [old]


def test_fresh_completed_result_takes_precedence(match):
    old = replace(match, state="completed", team1_score=2, team2_score=1)
    fresh = replace(old, team1_score=3, stable_uid="changed")
    merged = merge_matches([fresh], [match_to_dict(old)], "Europe/Berlin")
    assert merged[0].team1_score == 3
    assert merged[0].stable_uid == old.stable_uid


def test_reschedule_preserves_uid_and_deduplicates_by_league(match):
    fresh = replace(
        match, match_start_utc=match.match_start_utc + timedelta(days=1), stable_uid="new"
    )
    other = replace(match, league_slug="lck", stable_uid="other-league")
    merged = merge_matches([fresh, other], [match_to_dict(match)], "Europe/Berlin")
    assert len(merged) == 2
    assert next(m for m in merged if m.league_slug == "lec").stable_uid == match.stable_uid
    assert next(m for m in merged if m.league_slug == "lck").stable_uid == "other-league"


def test_url_identity_is_consistent_for_html_matches(match):
    html_match = replace(match, match_id=None)
    assert canonical_key_for_match(html_match) == canonical_key_for_dict(match_to_dict(match))
    assert len(merge_matches([html_match], [match_to_dict(match)], "UTC")) == 1


def test_keeps_history_and_skips_malformed_entries(match, caplog):
    invalid = {**match_to_dict(match), "match_id": "bad", "match_start_utc": "not-a-date"}
    merged = merge_matches([], [match_to_dict(match), invalid], "UTC")
    assert len(merged) == 1
    assert "Skipping malformed history entry" in caplog.text


def test_store_removes_already_persisted_expired_rows(tmp_path, match):
    old = replace(
        match, match_start_utc=match.match_start_utc - timedelta(days=400), stable_uid="old"
    )
    with HistoryStore(str(tmp_path / "history.db")) as store:
        store.save([match_to_dict(old), match_to_dict(match)], retention_days=1000)
        assert len(store.load()["matches"]) == 2
        store.save([match_to_dict(old), match_to_dict(match)], retention_days=365)
        assert [m["stable_uid"] for m in store.load()["matches"]] == [match.stable_uid]


def test_store_upserts_and_rolls_back_on_error(tmp_path, match):
    with HistoryStore(str(tmp_path / "history.db")) as store:
        store.save([match_to_dict(match)])
        changed = match_to_dict(replace(match, team1_score=2))
        store.save([changed])
        assert store.load()["matches"][0]["team1_score"] == 2
        with pytest.raises(sqlite3.IntegrityError):
            store.save(
                [{**changed, "team1_score": 3}, {**changed, "stable_uid": "bad", "team1": None}]
            )
        assert store.load()["matches"][0]["team1_score"] == 2


def test_unchanged_history_does_not_rewrite_database(tmp_path, match):
    with HistoryStore(str(tmp_path / "history.db")) as store:
        data = [match_to_dict(match)]
        store.save(data)
        changes = store.conn.total_changes
        store.save(data)
        assert store.conn.total_changes == changes


def test_dict_accepts_z_dates(match):
    data = match_to_dict(match)
    data["match_start_utc"] = data["match_start_utc"].replace("+00:00", "Z")
    assert dict_to_match(data, "Europe/Berlin").match_start_utc == match.match_start_utc


def test_json_migration(tmp_path, match):
    import json

    source = tmp_path / "custom.json"
    source.write_text(json.dumps({"matches": [match_to_dict(match)]}), encoding="utf-8")
    with migrate_json_to_sqlite(source, str(source.with_suffix(".db"))) as store:
        assert len(store.load()["matches"]) == 1
    assert source.with_suffix(".json.bak").exists()
