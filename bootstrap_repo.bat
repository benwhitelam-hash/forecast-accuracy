@echo off
REM =============================================================
REM  One-shot: init git, push to a NEW private repo on GitHub.
REM
REM  No gh CLI required. You'll be asked once to create the empty
REM  repo on github.com (we'll open the page for you), then this
REM  script does the rest using plain git over HTTPS - your
REM  existing GitHub credentials (the same ones you push
REM  Agile-Alerter with) will be reused automatically by Git
REM  Credential Manager.
REM =============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo == Checking git is available ==
git --version || goto :fail_with_pause

echo.
echo == Checking we're in the forecast-accuracy folder ==
if not exist "forecast_accuracy\app.py"     ( echo ERROR: not in project folder & goto :fail_with_pause )
if not exist ".github\workflows\refresh.yml" ( echo ERROR: workflow file missing & goto :fail_with_pause )

echo.
echo == git init (if needed) ==
if not exist ".git" (
    git init -b main || goto :fail_with_pause
) else (
    echo .git already exists, skipping init
)

echo.
echo == Staging files ==
git add .gitignore .gitattributes README.md CLAUDE.md requirements.txt requirements-collect.txt run.sh start_streamlit.bat bootstrap_repo.bat forecast_accuracy .github data/forecast_accuracy.sqlite || goto :fail_with_pause

echo.
echo == Initial commit ==
git diff --cached --quiet
if %errorlevel% equ 0 (
    echo Nothing new to stage. Either already committed, or files unchanged.
) else (
    git commit -m "Initial commit: forecast-accuracy engine + refresh workflow" || goto :fail_with_pause
)

echo.
echo ==============================================================
echo == MANUAL STEP - create the empty remote repo on GitHub     ==
echo ==============================================================
echo.
echo Your browser will open to github.com/new in 3 seconds.
echo Please use these EXACT settings:
echo.
echo     Owner:          benwhitelam-hash
echo     Repository:     forecast-accuracy
echo     Visibility:     Private
echo     Initialize:     LEAVE ALL CHECKBOXES UNCHECKED
echo                     (no README, no .gitignore, no license)
echo.
echo Then click "Create repository" and come back here.
echo.
timeout /t 3 /nobreak >nul
start "" "https://github.com/new?name=forecast-accuracy&description=AgilePredict+vs+Octopus+Agile+accuracy+tracker"

echo.
echo Waiting for you to finish creating the repo...
pause

echo.
echo == Wiring remote and pushing ==
git remote remove origin >nul 2>&1
git remote add origin https://github.com/benwhitelam-hash/forecast-accuracy.git || goto :fail_with_pause
git branch -M main
git push -u origin main || (
    echo.
    echo Push failed. Common causes:
    echo   - Repo doesn't exist yet (did you click "Create repository"?)
    echo   - Repo name is different (must be exactly "forecast-accuracy")
    echo   - GitHub credentials need refreshing - try running `git push` from
    echo     the Agile-Alerter repo folder to re-prompt Credential Manager,
    echo     then re-run this script.
    goto :fail_with_pause
)

echo.
echo == Done ==
echo.
echo Repo:    https://github.com/benwhitelam-hash/forecast-accuracy
echo Actions: https://github.com/benwhitelam-hash/forecast-accuracy/actions
echo.
echo The cron will fire automatically at roughly 06:20 / 10:20 / 16:20 / 22:20 UK.
echo To trigger the FIRST run immediately, visit the Actions tab, click
echo "refresh-data" in the left sidebar, then "Run workflow".
echo.
pause
exit /b 0

:fail_with_pause
echo.
echo ==========================================================
echo BOOTSTRAP FAILED - scroll up to see the specific error.
echo ==========================================================
echo.
pause
exit /b 1
