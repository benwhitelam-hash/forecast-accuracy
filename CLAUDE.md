# Claude notes — forecast-accuracy

Instructions for Claude (Cowork/Claude Code) working on this project.

## Where the canonical state lives

Repo: `benwhitelam-hash/forecast-accuracy` (private). The SQLite database
`data/forecast_accuracy.sqlite` is **tracked in git** — that is the
authoritative history. A GitHub Actions workflow at
`.github/workflows/refresh.yml` runs the collectors 8×/day (four UK target
times, each scheduled for both GMT and BST UTC equivalents) and commits
updates back.

Consequences:
- Before editing locally, `git pull` so you don't conflict with a cron-pushed
  commit. The workflow commits as `github-actions[bot]`.
- Hot-reload-driven edits to `forecast_accuracy/*.py` are safe — you're
  only changing code, not data. Streamlit's file watcher picks them up
  on the next interaction.
- If you change `storage.py` schema, you'll need a migration story. Current
  schema is `CREATE ... IF NOT EXISTS` only, so adding *new* columns/tables
  is safe; renaming/dropping is not.

## Launching the Streamlit app autonomously

The one-shot launcher is `start_streamlit.bat` at the project root. It activates
the existing Windows `.venv` and runs `streamlit run forecast_accuracy/app.py
--server.headless true --server.port 8501`.

**In a fresh Cowork chat, to get the app running and visible in Chrome:**

1. **Mount the project folder.** Call `mcp__cowork__request_cowork_directory`
   with
   `path=C:\Users\benwh\OneDrive\Documents\Claude\Projects\Agile Alerter\forecast-accuracy`.
   It mounts at `/sessions/<session>/mnt/forecast-accuracy`.

2. **Request computer-use access.** Call `mcp__computer-use__request_access`
   for `["File Explorer", "Command Prompt", "Google Chrome"]`. Expect tiers:
   File Explorer = full, Command Prompt = click, Google Chrome = read.
   That's fine for this task — see gotchas below.

3. **Open File Explorer, navigate, run the .bat.**
   - `open_application("File Explorer")`
   - `left_click` somewhere in the File Explorer window to ensure it's focused
     (the address bar trick needs focus).
   - `key("ctrl+l")` to focus the address bar; the current path highlights.
   - `type("C:\\Users\\benwh\\OneDrive\\Documents\\Claude\\Projects\\Agile Alerter\\forecast-accuracy")`
     then `key("Return")`.
   - Take a screenshot, locate `start_streamlit.bat` in the file list
     (usually last row, Type = "Windows Batch File").
   - `double_click` its row. A cmd window pops open top-left of the screen
     showing "Starting Streamlit on http://localhost:8501".
   - Wait ~5s for streamlit to bind the port.

4. **Open Chrome and navigate.**
   - `mcp__computer-use__open_application("Google Chrome")`.
   - Load Chrome MCP tools via ToolSearch: `{query: "chrome", max_results: 25}`.
   - `mcp__Claude_in_Chrome__tabs_context_mcp` with `createIfEmpty=true`. If
     this returns "Chrome extension is not connected", wait 3s and retry —
     it takes a moment after Chrome opens.
   - `mcp__Claude_in_Chrome__navigate` to `http://localhost:8501` on the
     returned tabId.
   - Optional: `mcp__Claude_in_Chrome__resize_window` to 1280×900 for a
     better screenshot.

5. **Screenshotting.** Streamlit holds a websocket open, so Chrome MCP's
   screenshot tool errors with "Page still loading (executeScript waited
   45000ms for document_idle)". **Workaround:** use
   `mcp__computer-use__screenshot` instead — it captures the whole desktop
   including the Chrome window, which is fine since Chrome is tier-read
   (visible in screenshots, just can't be clicked).

## Iterating on the code while the app is running

Streamlit's hot-reload watches for file changes. Edit files under
`forecast_accuracy/` with the `Edit` tool and the running app picks up the
change on the next interaction (there's a "Rerun" prompt at the top-right, or
tap `R` in the browser). No need to restart the cmd window between edits.

## Running the collectors from the Cowork sandbox

If you want to run `python -m forecast_accuracy.cli refresh` or similar from
the Linux sandbox (rather than the Windows venv), the FUSE mount doesn't
support SQLite file locking. Workaround:

```bash
# one-time
pip install -r requirements.txt --break-system-packages --quiet

# copy DB out, run against it, copy back
cp "/sessions/<session>/mnt/forecast-accuracy/data/forecast_accuracy.sqlite" /tmp/
cd "/sessions/<session>/mnt/forecast-accuracy"
FORECAST_ACCURACY_DB=/tmp/forecast_accuracy.sqlite \
    python -m forecast_accuracy.cli refresh
cp /tmp/forecast_accuracy.sqlite "/sessions/<session>/mnt/forecast-accuracy/data/"
```

The `FORECAST_ACCURACY_DB` env var is the designed escape hatch (see
`storage.py` — it's documented).

## Tool-tier gotchas

- **Google Chrome = tier "read".** Screenshots work; clicks and typing are
  blocked. **Never** try to drive Chrome with computer-use clicks. All
  navigation and interaction must go through `mcp__Claude_in_Chrome__*`.
- **Command Prompt = tier "click".** You can see the cmd window running
  streamlit and left-click in it (e.g. to close), but can't type. For shell
  work, use the Bash tool against the sandbox, not computer-use on cmd.
- **File Explorer = tier "full".** No restrictions. Use it to navigate and
  double-click files, including .bat files.

## Stopping the app

Close the cmd window (X button, tier-click means `left_click` works on it),
or the user can Ctrl+C inside it.

## Project essentials (duplicated from memory for convenience)

- Forecast source: AgilePredict (`prices.fly.dev/api/G/`). Refreshes 4×/day
  UK at 06:15, 10:15, 16:15, 22:15. Does not serve history — accuracy data
  must be built by polling.
- Primary outturn: Octopus Agile (apples-to-apples p/kWh inc VAT).
- Secondary outturn: Elexon BMRS MID (APXMIDP). Wholesale £/MWh stand-in
  for ENTSO-E (whose API registration is deferred).
- MAPE excludes rows with |outturn| < 2 p/kWh (near-zero blow-up). MAE is
  reported alongside.
- Horizon buckets: 0–24h, 24–48h, 48–72h, 72–120h, 120h+.
- Default region: G (London).
