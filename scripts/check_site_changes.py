"""Compare generated site files with their versioned predecessors, ignoring DTSTAMP."""

import subprocess
import sys
from pathlib import Path


def normalize_feed_for_comparison(feed: str) -> str:
    return "\n".join(line for line in feed.splitlines() if not line.startswith("DTSTAMP:"))


def site_changed(directory: Path) -> bool:
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".ics", ".json", ".html"}:
            continue
        previous = subprocess.run(
            ["git", "show", f"HEAD:{path.as_posix()}"],
            capture_output=True,
            check=False,
        )
        if previous.returncode:
            return True
        old = previous.stdout.decode("utf-8")
        new = path.read_text(encoding="utf-8")
        if path.suffix == ".ics":
            old = normalize_feed_for_comparison(old)
            new = normalize_feed_for_comparison(new)
        if old != new:
            return True
    return False


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_site")
    print(f"changed={str(site_changed(directory)).lower()}")
