@echo off
rem Launch the job-crawler web UI from source (needs Python + `pip install -r
rem requirements.txt`). Opens your browser once the server is up. Safe to
rem double-click twice: a second launch just opens a tab to the running app.
cd /d "%~dp0"
where python >nul 2>&1
if %errorlevel%==0 (
  python webapp.py --open
) else (
  py -3 webapp.py --open
)
if errorlevel 1 (
  echo.
  echo If the error above is a missing module ^(e.g. flask^), install the
  echo requirements into THIS python first:  pip install -r requirements.txt
  echo If python itself was not found, use the standalone build instead:
  echo run build_exe.bat once, then launch webapp.dist\JobCrawlerUI.exe.
)
pause
