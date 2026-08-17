@echo off
rem Windows convenience wrapper - the build itself lives in build_app.py
rem (same command on every OS; CI builds Windows/macOS/Linux with it). Runs in
rem the `jobs` conda environment, so the binary bundles those dependencies.
rem
rem     JobCrawlerUI.exe
rem
rem See build_app.py's docstring for what gets bundled, how the app finds
rem its data at runtime, and why Playwright is excluded.
rem
rem ASCII only - see the note in activate_env.bat.
cd /d "%~dp0"
call "%~dp0activate_env.bat"
if errorlevel 1 goto :end
python build_app.py
:end
pause
