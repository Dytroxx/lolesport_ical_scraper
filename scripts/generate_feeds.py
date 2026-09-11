"""Split a combined iCal feed into per-league feeds."""
import sys
from pathlib import Path

def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_site/feed.ics")
    leagues = ["lec", "lck", "lpl", "lcs", "worlds", "msi", "emea_masters", "first_stand"]

    with open(input_path) as f:
        lines = f.readlines()

    for lg in leagues:
        vevents = []
        in_ve = False
        buf = []
        for line in lines:
            if line.strip() == "BEGIN:VEVENT":
                in_ve = True
                buf = [line]
            elif line.strip() == "END:VEVENT":
                buf.append(line)
                if in_ve:
                    full = "".join(buf)
                    if f"league_slug={lg} " in full or f"league_slug={lg}\n" in full or f"X-LOLES-LG:{lg}" in full:
                        vevents.append(full)
                    in_ve = False
                    buf = []
            elif in_ve:
                buf.append(line)

        if vevents:
            out_path = Path(f"_site/feed-{lg}.ics")
            with open(out_path, "w") as out:
                out.write("BEGIN:VCALENDAR\n")
                out.write("VERSION:2.0\n")
                out.write("PRODID:-//LoL Esports iCal//DE\n")
                out.writelines(vevents)
                out.write("END:VCALENDAR\n")
            print(f"[split] {out_path.name}: {len(vevents)} events")

if __name__ == "__main__":
    main()
