from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import Match
from .util import ensure_tzaware_utc


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


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


def _estimate_match_duration(best_of: str | None) -> timedelta:
    """Estimate match duration based on best-of format."""
    if best_of == "Bo5":
        return timedelta(hours=4)
    elif best_of == "Bo3":
        return timedelta(hours=2, minutes=30)
    else:  # Bo1 or unknown
        return timedelta(hours=1, minutes=30)


def render_ical(matches: Iterable[Match], *, prodid: str = "-//lolesports-ical//EN") -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"PRODID:{_ics_escape(prodid)}",
        "X-WR-CALNAME:LoL Esports",
    ]

    for m in sorted(matches, key=lambda x: x.match_start_utc):
        # Use team codes for summary (short names), fall back to full names
        t1_display = m.team1_code or m.team1
        t2_display = m.team2_code or m.team2

        # Build summary with score if match is completed
        if m.state == "completed" and m.team1_score is not None and m.team2_score is not None:
            summary = f"[{m.league_name}] {t1_display} {m.team1_score}-{m.team2_score} {t2_display}"
        else:
            summary = f"[{m.league_name}] {t1_display} vs {t2_display}"

        # Build description with full team names
        desc_parts = [f"League: {m.league_name}"]
        desc_parts.append(f"Match: {m.team1} vs {m.team2}")
        if m.stage:
            desc_parts.append(f"Stage: {m.stage}")
        if m.best_of:
            desc_parts.append(f"Format: {m.best_of}")

        # Add result info for completed matches
        if m.state == "completed":
            if m.team1_score is not None and m.team2_score is not None:
                desc_parts.append(f"Result: {m.team1} {m.team1_score} - {m.team2_score} {m.team2}")
            if m.winner:
                desc_parts.append(f"Winner: {m.winner}")
        elif m.state == "inProgress":
            desc_parts.append("Status: LIVE")

        description = "\n".join(desc_parts)

        # Calculate end time based on best-of format
        match_duration = _estimate_match_duration(m.best_of)
        match_end_utc = m.match_start_utc + match_duration

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{_ics_escape(m.stable_uid)}",
            f"DTSTAMP:{_dt_to_ics_utc(now)}",
            f"DTSTART:{_dt_to_ics_utc(m.match_start_utc)}",
            f"DTEND:{_dt_to_ics_utc(match_end_utc)}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
        ]
        if m.match_url:
            event_lines.append(f"URL:{m.match_url}")
        event_lines.append("END:VEVENT")

        for l in event_lines:
            lines.append(_fold_ics_line(l))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
