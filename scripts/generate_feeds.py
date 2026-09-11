"""Split a combined iCal feed into per-league feeds.

Extracts league info from VEVENT DESCRIPTION lines like:
  DESCRIPTION:League: EMEA Masters\\nMatch: ...
  DESCRIPTION:League: Worlds\\nMatch: ...
"""
import re
import sys
from pathlib import Path

# Map league display names (from DESCRIPTION) to output slug
LEAGUE_NAMES: dict[str, str] = {
    "EMEA Masters": "emea_masters",
    "First Stand": "first_stand",
    "LCK": "lck",
    "LCS": "lcs",
    "LEC": "lec",
    "LPL": "lpl",
    "MSI": "msi",
    "Worlds": "worlds",
}


def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("feed.ics")
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        print(f"[split] Error: {input_path} not found")
        sys.exit(1)

    with open(input_path) as f:
        lines = f.readlines()

    # Collect events per league
    league_events: dict[str, list[str]] = {slug: [] for slug in LEAGUE_NAMES.values()}
    in_ve = False
    buf: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            in_ve = True
            buf = [line]
        elif stripped == "END:VEVENT":
            buf.append(line)
            if in_ve:
                full = "\n".join(buf)
                # Parse league from DESCRIPTION: "League: EMEA Masters\\n..."
                desc_match = re.search(r"DESCRIPTION:League:\s*(.+?)(?:\\n|\\N|\n|$)", full)
                if desc_match:
                    league_name = desc_match.group(1).strip()
                    slug = LEAGUE_NAMES.get(league_name)
                    if slug:
                        league_events[slug].append(full)
            in_ve = False
            buf = []
        elif in_ve:
            buf.append(line)

    # Write per-league feeds
    for slug, events in league_events.items():
        if events:
            out_path = out_dir / f"feed-{slug}.ics"
            with open(out_path, "w") as out:
                out.write("BEGIN:VCALENDAR\n")
                out.write("VERSION:2.0\n")
                out.write("PRODID:-//LoL Esports iCal//EN\n")
                out.write("X-WR-CALNAME:LoL Esports - " + slug.upper() + "\n")
                out.writelines(events)
                out.write("END:VCALENDAR\n")
            print(f"[split] {out_path.name}: {len(events)} events")


if __name__ == "__main__":
    main()
