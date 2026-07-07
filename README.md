# Onsale Page View Comparison Dashboard

Compares page views (and queue demand) across Live Nation onsales/shows, pulled live from the
**Live Nation Onsale Reports** Google Drive folder on a schedule, parsed and analyzed by Claude.

## How it works (current setup)

```
Google Drive (onsale report sheets)
        │  Claude scheduled task runs daily (see "Scheduled" in Cowork)
        ▼
Claude reads each changed sheet directly — no Google Cloud project or
service account needed, it reuses Claude's own already-authorized Drive
access — and extracts normalized records into data/onsales.json
        │
        ▼
Claude compares shows against data/venue-baselines.json and writes
precomputed commentary (including 12h/24h "early read" callouts) into
data/insights.json
        │
        ▼
Charlie runs a quick `git push` to publish the refresh (see below —
this connector currently can't push to GitHub on its own)
        │
        ▼
GitHub Pages serves index.html, which reads both JSON files client-side
```

The sheets in that Drive folder are hand-maintained by different people, so column layouts,
date formats, and hour buckets (+12h/+24h/+72h/+96h) vary from tour to tour, and some sheets have
other artists' figures pasted in purely as benchmarks. Rather than write a brittle regex/pandas
parser that breaks every time someone changes a sheet's layout, Claude reads each sheet directly
and applies judgement to normalize it — the same way a person would. Claude also writes a short
list of insights each sync (pacing vs. venue benchmark, notable anomalies, and dedicated
12h/24h "early read" comparisons for freshly-announced shows) — that's what shows up in the
"Claude analysis" panel on the dashboard.

**Why not the GitHub Action shown in `scripts/` + `.github/workflows/`?** That path needs a
Google Cloud service account, which requires creating a Google Cloud project — something locked
down by IT policy on a standard Ticketmaster Google account. The scheduled-task approach above
sidesteps that entirely by reusing Claude's own already-granted Drive access instead. The
GitHub Action files are left in the repo as an optional upgrade path if IT provisions a GCP
project later — see "Advanced" below. It would also need updating to know about the
`venue_type`/pacing/tour-rollup features added since it was written, since it currently only
writes the older flat insight format.

### Distribution: Slack

The real workflow this dashboard supports: when a new arena/stadium tour is announced, a
coordinator creates a new sheet in the Drive folder and manually types in each show's page view
numbers 12h/24h after announce. So the sync runs **hourly, 9am-7pm on weekdays**
(`cronExpression: "0 9-19 * * 1-5"`) rather than daily, to catch that entry promptly. As soon as
it spots a genuinely new early-read insight, it posts it straight to **#emea-pageview-updates**
on Slack, so the tour team sees it without needing to open the dashboard — your team can then
copy/paste from Slack or the dashboard into an email as usual. Each show's early read is only
posted once (tracked in `data/_posted_early_reads.json`); if its numbers update again later the
same day, that update won't trigger a second Slack post — a known simplification for now.

**Caveat:** Cowork scheduled tasks only run while the Cowork app is open. Hourly, same-day
commentary during the workday depends on someone having the app open on their machine during
that window — it isn't a server running independently in the background.

### Benchmarking methodology

Both the scheduled sync's Claude-written commentary and the dashboard's instant on-page compare
panel follow the same comparison-basis priority, so a "benchmark" here means something specific
rather than just an average of whatever's on file:

1. **Venue match first.** Different artists who played the *same venue* are the strongest
   comparison — it holds city, capacity, and local market constant. The instant compare panel
   surfaces these as "Strongest comparison basis" matchups when 2+ selected artists share a
   venue; the scheduled sync's commentary does the same when writing early-read/general insights.
2. **Then sales stage, time window, and report type.** Within a same-venue comparison, +24h EDP
   views are compared to +24h EDP views (not +72h), presale queue to presale queue, etc.
3. **Artist/genre/audience/market similarity as a fallback**, only used when same-venue history
   is too thin to say something concrete — and the commentary says so explicitly when it falls
   back to this tier.

The Claude-written commentary also: states its comparison basis whenever it's inferred (always
true for an unattended job), calls out gaps/contradictions that limit a clean comparison (e.g.
FR/IT's non-comparable queue systems, a single-data-point venue), and explicitly distinguishes a
one-report observation from a small-sample comparison from a stronger multi-event pattern —
it will not imply a trend from one number. It may use a sparing, disclosed web search to help
pick sensible touring-peer comparisons when the report data alone doesn't make the set obvious,
but outside context never overrides what the reports actually show.

## Dashboard features

- **By show / By tour toggle** — "By show" is the original one-row-per-date view. "By tour"
  groups every show under the same `tour` field (or `artist (year)` when a record has no tour
  set) into one rollup row: total/average views, venues covered, average queue, and a pacing
  breakdown across the tour's shows. Use this for a one-line tour team update instead of
  scrolling a full show list.
- **Venue type filter** — venues are classified as `stadium` (40k+ cap), `arena` (8k-40k), or
  `theatre_club` (<8k) from the capacity noted in `data/venue-baselines.json`. Combine this with
  the artist compare picker to line up stadium-level acts against each other specifically,
  rather than mixing in small-venue runs.
- **Pacing symbols (▲ / ● / ▼)** — each show is compared against the historical average for
  *other* shows at that same venue (computed live from `data/onsales.json`, not a fixed number):
  ▲ ahead (15%+ above), ● on par (within 15%), ▼ below (15%+ under). Hover a symbol for the exact
  percentage and sample size it's based on. Tour rollups show a count of each across that tour's
  shows.
- **Early-read commentary** — when a show has only +12h or +24h data (i.e. it was just
  announced), the scheduled sync asks Claude to specifically compare it against the venue's
  historical average *at that same hour mark* and against 2-3 peer artists at similarly-sized
  venues, and tags it `category: "early_read"` in `data/insights.json`. These show up
  highlighted (amber border) at the top of the "Claude analysis" panel, ahead of general
  commentary — this is what the tour team should check 12-24h after an announcement.
- **Executive summary + trajectory in the Commentary tab** — selecting 1 artist/tour or 2+ for
  comparison now leads with a paste-ready sentence (comparison basis + demand readout, e.g. "BTS's
  8 shows are tracking ahead of the venue-history average by 12% — strong, steady growth across
  the windows on file") plus a "Copy summary" button, a benchmark delta stat (e.g. "+12% vs venue
  avg"), and a 12h→96h trajectory sparkline showing whether views are still climbing, front-loaded
  and cooling, or steady — all computed deterministically from `data/onsales.json`, no live API
  call needed. Trajectory only appears for EDP/ADP metrics (queue metrics are single peak values,
  no hour-by-hour shape to show).
- **Data quality flags kept separate from demand commentary** — every insight is tagged
  `category: "data_quality" | "early_read" | "general"`. `data_quality` covers problems with the
  source sheets themselves (wrong file contents, duplicated figures, mismatched labels) rather
  than a read on demand, and is never phrased as if it tells you something about ticket demand.
  In the Page View Commentary tab these render collapsed under a separate "data quality flags"
  section (or as a clearly-labeled "caveats" note when relevant to a selected artist/comparison)
  instead of being mixed in with real commentary — so nothing broken gets mistaken for something
  safe to repeat to a client.

## Repo contents

- `index.html` — the dashboard (Chart.js from CDN, no build step, works as-is on GitHub Pages)
- `data/onsales.json` — normalized per-show page view / queue records
- `data/venue-baselines.json` — per-venue benchmark bands used for comparison
- `data/insights.json` — Claude's precomputed commentary (created after the first sync run)
- `data/_sync_manifest.json` — tracks which Drive files have already been parsed, so unchanged
  sheets aren't re-sent to Claude on every run (created after the first sync run)
- `scripts/sync_data.py` + `.github/workflows/sync-onsale-data.yml` — an alternative, fully
  self-hosted sync path (see "Advanced" below); not currently active
- `data/_sync_manifest.json` — only relevant to the GitHub Action path above (the scheduled-task
  path tracks the same thing per-record via `source_modified_time` inside onsales.json instead)

The current `data/onsales.json` and `data/venue-baselines.json` are **seed data**, hand-derived
from prior research into these reports (117 shows, 46 venues) so the dashboard has something to
show immediately. Live syncs replace/merge with this over time.

## Setup

1. **Enable GitHub Pages**: Settings → Pages → Source: "Deploy from a branch" → Branch: `main` /
   `(root)` → Save. GitHub will give you a URL like
   `https://charlieogbechie.github.io/onsale-pageview-dashboard/`.
2. That's it for the current setup — no API keys or secrets required. The scheduled Claude task
   ("onsale-dashboard-sync" in Cowork's Scheduled section) handles pulling fresh data on its own
   cadence (currently daily). It updates the local data files but can't push to GitHub itself, so
   after it runs, publish the refresh with:
   ```
   cd "/Users/charlie.ogbechie/Documents/Claude/Projects/Page view anaylsis dashboard/onsale-pageview-dashboard" && git add . && git commit -m "Sync onsale data" && git push
   ```

## Advanced: fully automated via GitHub Actions (optional, needs IT)

If/when a Google Cloud project + service account becomes available (e.g. IT provisions one),
you can switch to the fully self-hosted path instead of relying on a Claude scheduled task:

1. Enable the **Google Drive API** and **Google Sheets API** on that GCP project.
2. Create a service account, generate a JSON key, and share the **Live Nation Onsale Reports**
   Drive folder with its email address (Viewer access).
3. Add three GitHub Actions secrets (Settings → Secrets and variables → Actions):
   - `GDRIVE_SERVICE_ACCOUNT_JSON` — the full service account JSON key
   - `GDRIVE_FOLDER_ID` — `1RRmWiaGlTFzLzLXmty-rju4Dh_20uCVF`
   - `ANTHROPIC_API_KEY` — a Claude API key (console.anthropic.com)
4. The workflow in `.github/workflows/sync-onsale-data.yml` will then run automatically every
   6 hours, or on demand from the Actions tab. Note: as of this writing it produces the older
   flat insight format (no `category`/early-read split) — bring it in line with the scheduled
   task's prompt (ask Claude to update it) before relying on it long-term.

## "Ask a question" — live Q&A setup

The dashboard has an "Ask a question" box that calls Claude live, answering from the current
`data/onsales.json` / `venue-baselines.json` / `insights.json` (not raw Drive sheets directly —
giving the backend its own Drive access would hit the same Google Cloud/IT-policy wall described
above, so it works from the same structured data the rest of the dashboard uses, refreshed
hourly by the scheduled sync). It follows a specific methodology: venue-match-first benchmarking,
insight-first structured answers (key insights, comparison basis, trends, risks/gaps, demand
readout, reports used), and a lightweight cross-question "memory" of established comparison sets
— see `worker/worker.js` for the exact system prompt if you want to tune it.

Since GitHub Pages can't hold an API key, this needs a small backend: a Cloudflare Worker.

### Deploy the Worker

1. Install Wrangler if you don't have it: `npm install -g wrangler`, then `wrangler login`
   (creates a free Cloudflare account if you don't have one — no IT approval needed).
2. From the `worker/` folder: `wrangler deploy`. Note the `*.workers.dev` URL it prints.
3. Set secrets (never go in `wrangler.toml` or git):
   ```
   wrangler secret put ANTHROPIC_API_KEY
   wrangler secret put SITE_KEY
   ```
   `SITE_KEY` can be any string you make up — see the risk callout below for why it exists.
4. Optional (for the "memory" feature): `wrangler kv namespace create BENCHMARK_MEMORY`, then
   uncomment and fill in the `[[kv_namespaces]]` block in `wrangler.toml` with the printed id,
   and `wrangler deploy` again. Without this, Q&A still works, it just won't remember benchmark
   context between separate questions.
5. In `index.html`, near the bottom of the `<script>` block, replace:
   - `ASK_ENDPOINT` with `https://<your-worker>.workers.dev/ask`
   - `ASK_SITE_KEY` with the same string you set as the `SITE_KEY` secret
6. Push and refresh the dashboard.

### ⚠️ Known risk: this is a public endpoint on a public repo

The repo (and therefore this JS, including whatever you put in `ASK_SITE_KEY`) is public, so
`SITE_KEY` is a scraping deterrent, not real security — anyone motivated enough can read it out
of the page source and call the Worker directly, which spends your Anthropic API quota. For a
small internal tool this is a reasonable tradeoff, but if usage/cost becomes a concern, consider:
Cloudflare's free rate-limiting rules on the Worker route, swapping in a real per-user auth
scheme, or taking the dashboard back to private hosting (see the GitHub Pages visibility tradeoff
earlier in this README).

## Extending this base

Ideas for a v4, not built yet:
- Per-tour trend lines (views over hours-since-announcement) rather than single-point comparisons.
- Slack/email digest of the precomputed insights (especially early-read ones) after each sync.
- Auto-push from the scheduled task once GitHub write access is sorted, removing the manual
  `git push` step entirely.
- Real per-user auth on the Q&A Worker instead of the shared "site key" deterrent.
