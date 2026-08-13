@echo off
rem Windows convenience wrapper — the build itself lives in build_app.py
rem (same command on every OS; CI builds Windows/macOS/Linux with it).
rem
rem     JobCrawlerUI.exe
rem
rem See build_app.py's docstring for what gets bundled, how the app finds
rem its data at runtime, and why Playwright is excluded.
cd /d "%~dp0"
python build_app.py
pause
