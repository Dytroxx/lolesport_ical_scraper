import subprocess
from pathlib import Path

import pytest

from scripts.check_site_changes import site_changed


@pytest.mark.parametrize(
    "name,old,new,expected",
    [
        ("feed.ics", "DTSTAMP:old\nUID:a\n", "DTSTAMP:new\nUID:a\n", False),
        ("feed-new-league.ics", "UID:a\n", "UID:b\n", True),
        ("leagues.json", "[]", '[{"slug":"new"}]', True),
        ("index.html", "old UI", "new UI", True),
        ("index.html", "same UI", "same UI", False),
    ],
)
def test_compares_actual_site_paths_and_normalizes_both_sides(
    tmp_path, monkeypatch, name, old, new, expected
):
    path = tmp_path / name
    path.write_text(new)
    calls = []

    def git_show(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=old.encode())

    monkeypatch.setattr(subprocess, "run", git_show)
    assert site_changed(tmp_path) is expected
    assert calls == [["git", "show", f"HEAD:{path.as_posix()}"]]


def test_new_file_requires_deployment(tmp_path, monkeypatch):
    (tmp_path / "leagues.json").write_text("[]")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 128, stdout=b""),
    )
    assert site_changed(tmp_path)
