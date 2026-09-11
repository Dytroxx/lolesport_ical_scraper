# LoL Esports iCal

[![Publish LoL Esports iCal](https://github.com/Dytroxx/lolesport_ical_scraper/actions/workflows/publish-ics.yml/badge.svg)](https://github.com/Dytroxx/lolesport_ical_scraper/actions/workflows/publish-ics.yml)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> Automatically scrape League of Legends esports match schedules and generate iCal feeds you can subscribe to from any calendar app.

## Quick Start

Add this URL to your calendar app (Google Calendar, Apple Calendar, Outlook, etc.):

```
https://dytroxx.github.io/lolesport_ical_scraper/feed.ics
```

### Per-League Feeds

Get individual feeds for specific leagues:

- [LEC](https://dytroxx.github.io/lolesport_ical_scraper/feed-lec.ics)
- [LCK](https://dytroxx.github.io/lolesport_ical_scraper/feed-lck.ics)
- [LPL](https://dytroxx.github.io/lolesport_ical_scraper/feed-lpl.ics)
- [LCS](https://dytroxx.github.io/lolesport_ical_scraper/feed-lcs.ics)

### Landing Page

Visit [dytroxx.github.io/lolesport_ical_scraper](https://dytroxx.github.io/lolesport_ical_scraper/) to browse leagues and generate your own custom subscription URLs.

## Features

- **All major leagues**: LEC, LCK, LPL, LCS, Worlds, MSI, EMEA Masters, First Stand
- **Match results** with final scores for completed games
- **Auto-updates** every 6 hours via GitHub Actions
- **Persistent history** — past matches stay in your calendar
- **Per-league feeds** for customized subscriptions
- **SQLite-backed** storage for efficient history management

## Self-Hosting

### Prerequisites

- Python 3.10 or later

### Installation

```bash
pip install lolesports-ical
```

Or install from source:

```bash
git clone https://github.com/Dytroxx/lolesport_ical_scraper.git
cd lolesport_ical_scraper
pip install -e .
```

### Usage

Generate an iCal feed for the next 30 days:

```bash
python -m lolesports_ical --out feed.ics
```

Generate a feed with custom settings:

```bash
python -m lolesports_ical --out feed.ics --tz Europe/Berlin --days 60 --leagues lec,lck
```

### Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--out` | Output `.ics` file path | `feed.ics` |
| `--tz` | Timezone for local match times | `Europe/Berlin` |
| `--days` | Days of matches to include | `30` |
| `--leagues` | Comma-separated league slugs (e.g., `lec,lck,lpl`) | All leagues |
| `--cache-dir` | Directory for HTTP response cache | `.cache/lolesports_ical` |
| `--cache-ttl` | Cache TTL in seconds | `1800` |
| `--history` | Path to JSON/SQLite history file | `data/history.json` |
| `--history-retention-days` | Keep history entries from last N days | `365` |

## How It Works

### Data Sources

The scraper uses two data sources, always preferring the more reliable one:

1. **LoL Esports API** (primary) — The unofficial Riot Games esports API (`esports-api.lolesports.com/persisted/gw`). This is the most accurate and up-to-date source.

2. **HTML Parser** (fallback) — If the API is unreachable, the scraper falls back to parsing the LoL Esports live page. This ensures you always get match data even during API outages.

### Architecture

```
┌─────────────────────────┐
│    API (Primary)        │  esports-api.lolesports.com
│    HTML Parser (Fallback)│  lol esports.com/live
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│   History Store         │  SQLite (data/history.db)
│   - Deduplication       │
│   - Auto-pruning (>365d)│
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│   iCal Generator        │  Generates .ics feed files
│   - feed.ics (combined) │
│   - feed-{league}.ics   │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│   GitHub Pages          │  Static hosting at
│   (auto-deploy)         │  dytroxx.github.io/lolesport_ical_scraper
└─────────────────────────┘
```

### Database

Match history is stored in SQLite (`data/history.db`) — significantly smaller and faster than the previous JSON-based storage. Matches older than 365 days are automatically pruned to keep the database small.

### CI/CD

The [GitHub Actions workflow](.github/workflows/publish-ics.yml) runs every 6 hours:

1. Fetches fresh match data from the API (with HTML fallback)
2. Merges new matches with SQLite history
3. Generates `feed.ics` and per-league `feed-{league}.ics` files
4. Updates `index.html` with the latest feeds
5. Commits changes and deploys to GitHub Pages

### API Configuration

- **Rate limiting**: 150ms between requests
- **Timeout**: 10s per API call
- **Cache TTL**: 120s (2 minutes) for fresh results
- **API Key**: Optional `LOL_API_KEYS` environment variable (hardcoded key used as public fallback)

## Project Structure

```
lolesport_ical_scraper/
├── lolesports_ical/          # Core package
│   ├── __main__.py           # CLI entry point
│   ├── main.py               # CLI + SQLite history
│   ├── api.py                # LoL Esports API client
│   ├── scrape.py             # HTML fallback parser
│   ├── ical.py               # iCalendar renderer
│   ├── models.py             # Match dataclass
│   └── util.py               # HTTP client, cache, rate limiter
├── scripts/
│   └── generate_feeds.py     # Split combined feed into per-league files
├── .github/workflows/
│   ├── publish-ics.yml       # CI/CD: generate + deploy feeds
│   └── cleanup-runs.yml      # Auto-prune old workflow runs
├── index.html                # Landing page with league selector
├── pyproject.toml            # Project configuration
└── README.md
```

## License

MIT License
