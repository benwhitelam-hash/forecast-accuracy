# Forecast Accuracy Engine

Measures how accurate [AgilePredict](https://agilepredict.com) is at predicting
Octopus Agile half-hourly prices, aggregated to monthly MAPE / MAE by
forecast horizon.

## Live app

Hosted on [Streamlit Community Cloud](https://share.streamlit.io). The app
auto-redeploys on every push to `main`, so the 8×/day CI refresh commits
drive the live app's data refresh with no manual step. See
[Deploying](#deploying) below.

## Why these sources

| Role | Source | Why |
| --- | --- | --- |
| Forecast | AgilePredict (`prices.fly.dev/api/G/`) | Only free, API-accessible 7–14 day HH GB Agile-price forecast found. |
| Primary outturn | Octopus Agile API | **Apples-to-apples** ground truth — same units (p/kWh inc VAT), same tariff, same region. |
| Secondary outturn | Elexon BMRS MID (APXMIDP) | Wholesale GBP/MWh cross-check, stand-in for ENTSO-E until a token arrives. Note N2EX (N2EXMIDP) is effectively zero-volume now; APX is the meaningful GB reference. |

AgilePredict predicts the Agile *retail* price (markup + VAT + cap already
baked in), so comparing to the wholesale day-ahead directly would give a
misleading MAPE. Octopus's own published Agile price is the correct ground
truth.

## Layout

```
forecast-accuracy/
├── .github/workflows/refresh.yml   # 8×/day cron → collectors → commit DB back
├── data/forecast_accuracy.sqlite   # tracked in git — this IS the history
├── forecast_accuracy/
│   ├── collectors/
│   │   ├── agilepredict.py         # forecasts → forecasts table
│   │   ├── octopus.py              # Agile outturn → outturn table
│   │   └── elexon.py               # wholesale day-ahead + within-day system price
│   ├── storage.py                  # SQLite schema + DAOs
│   ├── analysis.py                 # MAPE / MAE, horizon buckets, recent-prices query
│   ├── app.py                      # Streamlit UI
│   └── cli.py                      # `python -m forecast_accuracy.cli refresh`
├── requirements.txt                # full stack (Streamlit UI)
├── requirements-collect.txt        # lean stack for CI (just requests)
├── run.sh                          # venv + install + streamlit
└── README.md
```

## Running

```bash
./run.sh
# opens on http://localhost:8501
```

That's it. The script creates a venv on first run, installs deps, seeds a
refresh, and starts Streamlit.

## Scheduling — the autonomous setup

The whole point of this project is accumulating a forecast history, and
AgilePredict does not serve historical snapshots. So the repo is wired to
collect data on its own, via a GitHub Actions workflow that commits the
SQLite back after every run.

### How it works

`.github/workflows/refresh.yml` runs 8 times a day (4 UK local target times
× GMT-or-BST UTC equivalents): roughly 06:20 / 10:20 / 16:20 / 22:20 UK,
chosen to catch each AgilePredict refresh (06:15 / 10:15 / 16:15 / 22:15 UK)
and the ~16:00 UK Octopus Agile "tomorrow's prices" publish. Each run:

1. Installs only `requests` (collectors don't need Streamlit / pandas).
2. Runs `python -m forecast_accuracy.cli refresh --region G --days-back 7`.
3. Commits `data/forecast_accuracy.sqlite` back if it changed, then pushes.

Collectors are idempotent (`INSERT OR IGNORE` for forecasts,
`INSERT OR REPLACE` for outturn) so overlapping or repeated runs are safe.
Per-collector failures are caught in `cli.py` so a flaky upstream doesn't
kill the whole cron — the other collectors still commit their rows.

Manual runs: `gh workflow run refresh-data` (or the "Run workflow" button
on the Actions tab). You can override the backfill window with
`-f days_back=30`.

### The local Streamlit app

The UI still runs locally. The workflow commits the DB to `main`, so to
see the latest data, `git pull` before launching:

```bash
git pull
./run.sh                   # macOS / Linux
start_streamlit.bat        # Windows
# opens on http://localhost:8501
```

The **Refresh data** button in the sidebar is still useful for forcing an
immediate pull from the APIs on top of what CI has gathered.

### Cost and growth

Current DB footprint: ~800KB. Expected growth: ~30MB/year at full-bore
collection. SQLite-in-git is fine at this scale — if it ever becomes
unwieldy we can migrate to Turso/Supabase without changing the collector
shape (same `insert_forecasts` / `insert_outturn` DAO contract).

## Notes & caveats

- **First-run accuracy is sparse.** AgilePredict doesn't serve historical
  forecasts, so the MAPE chart only becomes meaningful once a few weeks of
  snapshots have accumulated.
- **MAPE near zero.** Agile prices can go near zero or negative on windy
  nights; MAPE on those rows blows up. The analysis module excludes rows
  with |outturn| < 2 p/kWh from MAPE and surfaces the exclusion count. MAE
  in p/kWh is reported alongside and is stable across the whole range.
- **Horizon buckets.** Forecast error grows with horizon. Buckets are
  0–24h, 24–48h, 48–72h, 72–120h, 120h+.
- **Snapshot selection.** "latest" keeps the freshest forecast for each
  target HH per bucket (realistic accuracy); "all" keeps every snapshot
  (useful for inspecting refinement across refreshes).
- **Regions.** Default region is G (London). Change in the sidebar.
- **ENTSO-E.** Registration is currently deferred — their API access
  requires manual approval (up to 3 business days). The Elexon APX row is
  the same underlying data, free and immediate.

## Deploying

Hosted on [Streamlit Community Cloud](https://share.streamlit.io). One-time setup:

1. Repo must be **public** (Community Cloud's free tier deploys only public repos).
2. Sign in to share.streamlit.io with GitHub and click **New app**.
3. Pick `benwhitelam-hash/forecast-accuracy`, branch `main`, main file
   `forecast_accuracy/app.py`. Python version 3.12 (matches CI).
4. No secrets / env vars needed — the app is read-only on the SQLite DB
   committed to the repo.

**Updating** is zero-touch: every push to `main` triggers Cloud to rebuild
the app. The 8×/day CI workflow (`.github/workflows/refresh.yml`) pushes
fresh DB commits, so the live app's data stays current automatically.

**Config** lives in `.streamlit/config.toml` (theme, telemetry off). Edit
and push to apply.
