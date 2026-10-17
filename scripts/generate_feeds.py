"""Split a feed using its league slugs and publish the league directory."""

import json
import re
import sys
from pathlib import Path


def generate_feeds(input_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "leagues.json"
    directory = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    leagues = {league["slug"]: league for league in directory}
    league_events: dict[str, list[str]] = {}
    buf: list[str] | None = None

    for line in input_path.read_text(encoding="utf-8").splitlines():
        if line == "BEGIN:VEVENT":
            buf = [line]
        elif buf is not None:
            if not line:
                continue
            buf.append(line)
            if line != "END:VEVENT":
                continue
            full = re.sub(r"\r\n[ \t]", "", "\r\n".join(buf))
            properties = dict(part.split(":", 1) for part in full.split("\r\n") if ":" in part)
            description = properties.get("DESCRIPTION", "")
            name_match = re.match(r"League:\s*(.*?)(?:\\n|\\N|$)", description)
            name = name_match.group(1) if name_match else ""
            slug = properties.get("X-LOL-LEAGUE-SLUG")
            if not slug:
                # Compatibility with feeds generated before explicit league slugs.
                slug = next(
                    (key for key, league in leagues.items() if league["name"] == name), None
                )
                slug = slug or re.sub(r"[^a-z0-9_-]+", "_", name.lower()).strip("_")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
                raise ValueError("Invalid or missing league slug in event")
            leagues.setdefault(slug, {"slug": slug, "name": name or slug, "region": ""})
            league_events.setdefault(slug, []).append("\r\n".join(buf))
            buf = None
    if buf is not None:
        raise ValueError("Unterminated VEVENT")

    for slug in leagues:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
            raise ValueError("Invalid league slug in directory")
        events = league_events.get(slug, [])
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "PRODID:-//LoL Esports iCal//EN",
            f"X-WR-CALNAME:LoL Esports - {slug}",
            *events,
            "END:VCALENDAR",
            "",
        ]
        (out_dir / f"feed-{slug}.ics").write_bytes("\r\n".join(lines).encode("utf-8"))
        print(f"[split] feed-{slug}.ics: {len(events)} events")

    directory = sorted(
        leagues.values(), key=lambda league: (league["name"].casefold(), league["slug"])
    )
    index_path.write_text(
        json.dumps(directory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("feed.ics")
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
    generate_feeds(input_path, out_dir)


if __name__ == "__main__":
    main()
