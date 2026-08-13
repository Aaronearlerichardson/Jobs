@echo off
rem Build a standalone folder distribution of the web UI with Nuitka:
rem
rem     webapp.dist\JobCrawlerUI.exe
rem
rem The dist folder is self-contained (bundled CPython + Flask + the crawler
rem packages + lxml etc.) - copy it to any Windows machine and double-click
rem the exe; no Python or pip installs needed there. Data resolution:
rem JOBS_DATA_DIR env var if set; else <exe dir>\data when it holds a DB;
rem else the exe's folder itself (legacy layout); else <parent>\data when
rem that holds jobs.db (dist inside this repo -> uses the repo's real
rem data); else a fresh <exe dir>\data. Set ANTHROPIC_API_KEY in the
rem environment for the scoring operations.
rem
rem Playwright (optional headless-browser probes for JS-only boards) is
rem excluded - it needs its own browser download and cannot ship in an exe;
rem those probes degrade gracefully.
rem
rem First build downloads a C compiler (MinGW64) if none is found and takes
rem a while (10-30 min); rebuilds are much faster.
cd /d "%~dp0"
python -m pip show nuitka >nul 2>&1 || python -m pip install nuitka
python -m nuitka webapp.py ^
  --standalone ^
  --output-filename=JobCrawlerUI.exe ^
  --include-package=core ^
  --include-package=scrapers ^
  --include-package=discovery ^
  --include-package=webapp ^
  --include-data-files=webapp/templates/index.html=webapp/templates/index.html ^
  --include-data-dir=webapp/static=webapp/static ^
  --include-data-files=profile.example.toml=profile.example.toml ^
  --assume-yes-for-downloads
if errorlevel 1 (
  echo.
  echo Build FAILED - see output above.
) else (
  echo.
  echo Build OK: webapp.dist\JobCrawlerUI.exe
)
pause
