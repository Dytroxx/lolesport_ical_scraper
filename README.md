# lolesports-ical

Production-grade-ish LoL Esports schedule scraper that emits an iCalendar feed.

## Install

From this folder:

```bash
python -m pip install -U pip
python -m pip install -e .
```

(Optional) For endpoint discovery via Playwright:

```bash
python -m pip install -e ".[playwright]"
python -m playwright install chromium
```

## Run

Defaults:
- leagues: `emea_masters,first_stand,lck,lcs,lec,lpl,msi,worlds`
- days: `30`
- tz: `Europe/Berlin`

```bash
python -m lolesports_ical --out feed.ics
```

Custom:

```bash
python -m lolesports_ical --out feed.ics --tz Europe/Berlin --days 30 --leagues emea_masters,lck,lec
```

If the unofficial API requires an API key, provide it:

```bash
setx LOLESPORTS_API_KEY "<key>"
python -m lolesports_ical --out feed.ics
```

## Hosting `feed.ics`

Any static file host works (nginx, GitHub Pages, S3, etc.). Point your calendar app at the URL of `feed.ics` as a subscription.

## Scheduled publishing via GitHub Actions + GitHub Pages

This repo includes a workflow that:
- runs the scraper on a schedule (cron)
- writes `site/feed.ics`
- deploys `site/` to GitHub Pages

### 1) Push this folder to GitHub

Create a repo (public or private) and push the contents of this directory.

### 2) Enable GitHub Pages (Actions)

In GitHub:
- **Settings → Pages**
- **Build and deployment → Source: GitHub Actions**

### 3) Workflow

The workflow is in [.github/workflows/publish-ics.yml](.github/workflows/publish-ics.yml).

You can run it manually via **Actions → Publish LoL Esports iCal → Run workflow**.

### 4) Subscribe to the calendar

Your feed will be available at:

`https://<github-user>.github.io/<repo>/feed.ics`

### Optional: API key secret

If you discover you need an API key for the unofficial `esports-api.lolesports.com` endpoints, add a repo secret:
- **Settings → Secrets and variables → Actions → New repository secret**
- Name: `LOLESPORTS_API_KEY`

The workflow already passes it through as an env var.

## Known limitations

- The site occasionally changes markup and/or internal API behavior. This project prefers a JSON schedule endpoint when accessible; otherwise it falls back to HTML parsing.
- HTML parsing is best-effort and relies on machine-readable `<time datetime="...">` when present.
- Some matches may have TBD teams; these are represented as `TBD`.
