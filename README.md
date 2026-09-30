# Client SEO Health Monitor

Pulls Search Console, GA4 and CrUX data for every configured client once a
day, compares each signal against a rolling baseline, and alerts when
something moves far enough for long enough to be real.

Runs entirely on GitHub Actions. Nothing depends on a local machine being
switched on.

---

## How it works

```
GitHub Actions (daily cron)
   └─ run.py
        ├─ collect   GSC + GA4 + CrUX  ──▶  data/history/<client>.json
        ├─ evaluate  baseline + thresholds ──▶ data/alerts.json
        ├─ notify    Slack
        └─ render    public/index.html
   └─ commits data/ and public/ back to the repo
        └─ Netlify serves public/ on push
```

### The baseline

A 7-day current window is compared against the prior 28 days divided by 4.
Twenty-eight days is exactly four of each weekday, so the comparison is
weekday-aligned by construction and a Monday is never measured against a
Saturday. This is the single biggest source of false alarms in naive
period-over-period monitors.

The history file is what makes this possible. A rolling comparison cannot be
computed from live API calls alone, so each run appends the day's numbers and
commits them. The file is small (daily totals only) and being in git means
every baseline the monitor ever used is recoverable.

### Why alerts are quiet

Four things keep the noise down:

1. **Persistence.** A signal must breach on N consecutive runs before it
   alerts. Clicks need 3, CTR needs 2. The streak survives between runs.
2. **Volume floors.** Properties under 100 baseline clicks a week and pages
   under 50 impressions are ignored.
3. **Data lag.** GSC finalises 2 to 3 days behind, and everything is pulled
   with `dataState=FINAL`, so a fresh incomplete day can never read as a drop.
4. **Deduplication.** An ongoing problem stays one open alert with a bumped
   counter, not one Slack message per day. It re-notifies only on escalation.

A signal that is breaching but has not yet met its persistence bar shows as
**Watching** on the dashboard rather than OK or No data.

### Why a failed collector is loud

If a source throws, the run records the failure, raises a critical
`monitor_failure_*` alert, skips the signals that depend on it, and exits
non-zero so the Actions run goes red. A silent gap must never be readable as
a clean bill of health.

---

## Signals

| Signal | Source | Cadence | Status |
|---|---|---|---|
| Organic clicks | GSC Search Analytics | Daily | Live |
| Organic impressions | GSC Search Analytics | Daily | Live |
| CTR divergence | GSC Search Analytics | Daily | Live |
| Indexation, priority URLs | GSC URL Inspection | Daily | Live |
| Indexation rate, sample | GSC URL Inspection | Daily | Live |
| Core Web Vitals | CrUX API | Weekly | Live, needs `CRUX_API_KEY` |
| LLM referrals | GA4 | Daily | Live |
| AI Overview impressions | GSC Generative AI report | — | **Not available via API** |
| Crawl stats | GSC Crawl Stats | — | **No public API**, needs server logs |
| Rich result snippets | URL Inspection | Weekly | Partial, see below |
| Shopping / Merchant Center | Content API | Daily | Not built, needs per-client access |

Three honest gaps:

- **AI Overview impressions.** Search Analytics does not expose the
  Generative AI report as its own search type; those impressions are folded
  into `web`. The GA4 LLM referral signal is built and working as the usable
  proxy. The GSC half stays a manual export until Google ships an API.
- **Crawl stats.** No public API. Needs server or CDN logs with verified
  Googlebot, or it stays a weekly manual export.
- **Rich results.** `searchAppearance` cannot be combined with `page` or
  `query` in one Search Analytics request, so you get appearance-level
  impressions or page-level detail, not both at once.

---

## Setup

Nothing needs installing locally. All of this is doable from the GitHub web
interface on any OS.

### 1. Create the repo

Upload these files to a new private GitHub repo.

### 2. Add the secrets

Settings → Secrets and variables → Actions → New repository secret.

| Secret | Required | What it is |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | The **entire contents** of the service account key file, pasted as one value |
| `SLACK_WEBHOOK_URL` | No | Slack incoming webhook. Without it, alerts print to the run log only |
| `CRUX_API_KEY` | No | Google Cloud API key with the Chrome UX Report API enabled. Without it, CWV is skipped |

The service account needs to be added as a user on each GSC property and each
GA4 property. If you already use that service account elsewhere, it is the
same identity and the same key, just stored here instead of on a local disk.

Enable these APIs in its Google Cloud project: Search Console API, Google
Analytics Data API, and Chrome UX Report API.

### 3. Seed the history

A cold start has no baseline, so the first 35 days would be silent. Run the
workflow once with a backfill instead:

Actions → SEO monitor → Run workflow → set **backfill** to `120`.

### 4. Connect Netlify

New site from Git, point it at the repo, publish directory `public`, no build
command. `netlify.toml` already sets this. Every monitor run pushes a commit,
which triggers a redeploy.

---

## Adding a client

Add a block to `clients.yaml` and commit. Nothing else.

```yaml
  clientslug:
    name: Client Name
    domain: www.example.co.za
    gsc_property: "https://www.example.co.za/"   # exact string from GSC
    ga4_property_id: "123456789"
    ecommerce: false
    seasonality: false
    priority_urls:
      - "https://www.example.co.za/"
      - "https://www.example.co.za/key-page"
    template_samples:
      home: "https://www.example.co.za/"
      category: "https://www.example.co.za/category"
    suppression_windows: []
    threshold_overrides: {}
```

`gsc_property` must match Search Console exactly, including the trailing
slash, or `sc-domain:example.co.za` for a domain property.

Then run the workflow once with a backfill so the new client starts with a
baseline rather than waiting five weeks.

### Tuning thresholds

Everything lives in `clients.yaml` under `defaults.thresholds`, overridable
per client via `threshold_overrides` using the same shape. No code change, no
redeploy.

### Suppressing known events

Migrations, sale launches and replatforms produce real movement that is not a
problem. Add a window and alerts inside it are logged but not sent:

```yaml
    suppression_windows:
      - start: "2026-11-24"
        end: "2026-12-02"
        reason: "Black Friday campaign"
```

---

## Running and testing locally

Optional. Only needed for tuning.

```bash
pip install -r requirements.txt

python run.py --client arcstore --dry-run       # evaluate, send nothing
python run.py --backfill 120                    # deep history pull
python run.py --no-collect --as-of 2026-09-18   # evaluate stored history
```

### Replaying history

`tools/replay.py` steps through stored history one day at a time, so
persistence counters behave exactly as they would in production. This is how
you tune a threshold: you can see which days would have alerted and, more
importantly, which quiet days would have stayed quiet.

```bash
python tools/replay.py arcstore --days 28
python tools/replay.py arcstore --drop 30 --from 2026-09-05
```

The second form injects a synthetic drop to confirm alerting fires when it
should. On the ArcStore pilot a 30% drop from 5 September first breaches on
10 September and alerts on 12 September, after exactly three consecutive
runs, with the top five losing pages attached.

---

## Layout

```
clients.yaml                config and thresholds
run.py                      entry point
monitor/
  config.py                 config loading, threshold resolution, suppression
  store.py                  history, snapshots, alert log
  collect.py                source orchestration and failure handling
  evaluate.py               baseline, thresholds, persistence, cause hints
  notify.py                 Slack
  render.py                 dashboard build
  sources/gsc.py            search analytics, URL inspection
  sources/ga4.py            organic sessions, LLM referrals
  sources/crux.py           Core Web Vitals
  templates/dashboard.html  the dashboard
tools/
  replay.py                 day-by-day replay for threshold tuning
  seed_history.py           one-off cold start from pulled data
  seed_snapshot.py          one-off page-level snapshot
data/
  history/<client>.json     daily totals, the baseline store
  snapshots/<client>.json   latest page-level detail, overwritten each run
  alerts.json               alert log with open/resolved status
public/index.html           the dashboard, served by Netlify
```
