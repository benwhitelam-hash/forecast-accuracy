#!/usr/bin/env bash
# Convenience launcher — works on Linux, macOS, and Windows Git Bash.
# Creates a venv, installs deps, starts Streamlit on localhost:8501.
set -euo pipefail
cd "$(dirname "$0")"

# ---- 1. Find a *working* Python 3 interpreter --------------------------------
# On Windows, `python3` may be the Microsoft Store shim that prints
# "Python was not found..." and exits non-zero. Actually execute each
# candidate to rule that out.
PYTHON_CMD=""
# Try candidates in order of preference.
# "py -3" is the Windows Python launcher; the Store shim is `python3`/`python`.
for cmd in "py -3" "python3" "python"; do
  # Intentional word-split: $cmd may be "py -3".
  # shellcheck disable=SC2086
  if $cmd -c "import sys; assert sys.version_info[0] == 3" >/dev/null 2>&1; then
    PYTHON_CMD="$cmd"
    break
  fi
done

if [ -z "$PYTHON_CMD" ]; then
  cat <<'EOF' >&2
No working Python 3 interpreter found.

On Windows: install Python from https://www.python.org/downloads/ (make sure
"Add python.exe to PATH" is ticked), then close and reopen Git Bash.
If `python3` opens the Microsoft Store instead of running Python, disable the
alias via: Settings → Apps → Advanced app settings → App execution aliases,
and untick the "python3.exe" and "python.exe" App Installer entries.
EOF
  exit 1
fi

echo "Using Python: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

# ---- 2. Create venv if missing ----------------------------------------------
# Check for the activate script, not just the directory — when running inside
# Docker with a named volume mounted at ./.venv, the directory exists but is
# empty on first spawn. Looking for activate lets us (re)create the venv
# *inside* the volume on first spawn, so subsequent spawns find it populated.
if [ ! -f .venv/bin/activate ] && [ ! -f .venv/Scripts/activate ]; then
  echo "Creating virtual environment in .venv ..."
  # shellcheck disable=SC2086
  $PYTHON_CMD -m venv .venv
fi

# ---- 3. Activate venv (Windows uses Scripts/, POSIX uses bin/) --------------
if [ -f .venv/Scripts/activate ]; then
  # shellcheck disable=SC1091
  source .venv/Scripts/activate
elif [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "Could not locate venv activate script under .venv/{Scripts,bin}/." >&2
  exit 1
fi

# ---- 4. Install deps --------------------------------------------------------
# First-run download of streamlit + pandas + altair can take 1-3 minutes.
# Show progress so you don't wonder if it's hung.
echo "Installing/updating Python dependencies (this can take a minute or two on first run)..."
pip install -r requirements.txt --progress-bar on --disable-pip-version-check

# ---- 5. Seed refresh so there's something to look at on first run -----------
echo "Seeding data from AgilePredict + Octopus ..."
python -m forecast_accuracy.cli refresh --no-elexon || true

# ---- 6. Launch Streamlit ----------------------------------------------------
echo "Starting Streamlit on http://localhost:8501 — Ctrl-C to stop."
exec streamlit run forecast_accuracy/app.py --server.headless true --server.port 8501
