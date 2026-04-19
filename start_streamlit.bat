@echo off
REM Launcher for the Forecast Accuracy Streamlit app.
REM Activates the existing Windows venv and starts streamlit on port 8501.
REM Close this window (or Ctrl-C) to stop the app.

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: .venv not found. Run ^`./run.sh^` once in Git Bash to create it.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

echo.
echo Starting Streamlit on http://localhost:8501
echo Press Ctrl-C to stop, or close this window.
echo.

streamlit run forecast_accuracy\app.py --server.headless true --server.port 8501

pause
