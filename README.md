# Onsale Page View Comparison Dashboard

Compares page views (and queue demand) across Live Nation onsales/shows, pulled live from the
**Live Nation Onsale Reports** Google Drive folder on a schedule, parsed and analyzed by Claude.

## How it works

```
Google Drive (onsale report sheets)
        │  every 6h, GitHub Action runs scripts/sync_data.py
        ▼
Claude API  ── extracts messy sheet data into data/onsales.json
        │
        ▼
Claude API  ── writes precomputed commentary into data/insights.json
        │
        ▼
GitHub Pages serves index.html, which reads both JSON files client-side
```

The sheets in that Drive folder are hand-maintained by different people, so column layouts,
date formats, and hour buckets (+12h/+24h/+72h/+96h) vary from tour to tour, and some sheets have
other artists' figures pasted in purely as benchmarks. Rather than write a brittle regex/pandas
parser that breaks every time someone changes a sheet's layout, `sync_data.py` sends each
changed sheet's raw contents to Claude with instructions on how to interpret these quirks, and
Claude returns normalized JSON records. Claude is also asked, at the end of each sync, to
compare shows against the venue benchmark bands in `data/venue-baselines.json` and write a short
list of insights — that's what shows up in the "Claude analysis" panel on the dashboard.

Precomputing the insights at sync time (rather than calling Claude live from the browser) means
the dashboard stays a plain static site with no exposed API key and no backend to host.

## Repo contents

- `index.html` — the dashboard (Chart.js from CDN, no build step, works as-is on GitHub Pages)
- `data/onsales.json` — normalized per-show page view / queue records
- `data/venue-baselines.json` — per-venue benchmark bands used for comparison
- `data/insights.json` — Claude's precomputed commentary (created after the first sync run)
- `data/_sync_manifest.json` — tracks which Drive files have already been parsed, so unchanged
  sheets aren't re-sent to Claude on every run (created after the first sync run)
- `scripts/sync_data.py` — the pull/parse/analyze script
- `.github/workflows/sync-onsale-data.yml` — the scheduled GitHub Action

The current `data/onsales.json` and `data/venue-baselines.json` are **seed data**, hand-derived
from prior research into these reports (117 shows, 46 venues) so the dashboard has something to
show immediately. Once the Action runs, live-parsed records replace/merge with this over time.

## Setup

### 1. Create a Google service account and share the Drive folder with it

1. In Google Cloud Console, create (or reuse) a project, then enable the **Google Drive API**
   and **Google Sheets API**.
2. Create a service account (IAM & Admin → Service Accounts), and generate a JSON key for it.
3. Open the **Live Nation Onsale Reports** folder in Google Drive, click Share, and add the
   service account's email address (looks like `name@project.iam.gserviceaccount.com`) with
   **Viewer** access.
4. Note the folder's ID from its URL: `drive.google.com/drive/folders/<FOLDER_ID>`.

### 2. Add GitHub Actions secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Secret name | Value |
|---|---|
| `GDRIVE_SERVICE_ACCOUNT_JSON` | the full contents of the service account JSON key file |
| `GDRIVE_FOLDER_ID` | the Drive folder ID from step 1.4 |
| `ANTHROPIC_API_KEY` | a Claude API key (console.anthropic.com) |

### 3. Enable GitHub Pages

**Settings → Pages → Source: Deploy from a branch → Branch: `main` / `(root)`**. GitHub will
give you a URL like `https://<you>.github.io/onsale-pageview-dashboard/`.

### 4. Run the sync

The Action runs automatically every 6 hours (`.github/workflows/sync-onsale-data.yml`). To run
it immediately: **Actions tab → Sync onsale page-view data → Run workflow**.

## Extending this base

Ideas for a v2, not built yet:
- Live in-browser Q&A (ask Claude a free-text question about the data) — needs a small serverless
  proxy (e.g. a Cloudflare Worker) to keep the API key off the client; the precomputed-insights
  approach here avoids that infra for the base version.
- Per-tour trend lines (views over hours-since-announcement) rather than single-point comparisons.
- Slack/email digest of the precomputed insights after each sync.
