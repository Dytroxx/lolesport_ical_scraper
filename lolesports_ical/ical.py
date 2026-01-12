from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .models import Match
from .util import ensure_tzaware_utc


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold_ics_line(line: str, limit: int = 75) -> str:
    # RFC5545 line folding: CRLF + single space continuation.
    if len(line) <= limit:
        return line
    out = []
    while len(line) > limit:
        out.append(line[:limit])
        line = " " + line[limit:]
    out.append(line)
    return "\r\n".join(out)


def _dt_to_ics_utc(dt: datetime) -> str:
    dt_utc = ensure_tzaware_utc(dt).astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")


def render_ical(matches: Iterable[Match], *, prodid: str = "-//lolesports-ical//EN") -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"PRODID:{_ics_escape(prodid)}",
    ]

    for m in sorted(matches, key=lambda x: x.match_start_utc):
        summary = f"[{m.league_name}] {m.team1} vs {m.team2}" if m.league_name else f"{m.team1} vs {m.team2}"
        desc_parts = [f"League: {m.league_name}"]
        if m.stage:
            desc_parts.append(f"Stage: {m.stage}")
        if m.best_of:
            desc_parts.append(f"Best-of: {m.best_of}")
        if m.match_url:
            desc_parts.append(f"URL: {m.match_url}")
        description = "\n".join(desc_parts)

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{_ics_escape(m.stable_uid)}",
            f"DTSTAMP:{_dt_to_ics_utc(now)}",
            f"DTSTART:{_dt_to_ics_utc(m.match_start_utc)}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
        ]
        if m.match_url:
            event_lines.append(f"URL:{_ics_escape(m.match_url)}")
        event_lines.append("END:VEVENT")

        for l in event_lines:
            lines.append(_fold_ics_line(l))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
