@echo off
rem Build a standalone folder distribution of the web UI with Nuitka:
rem
rem     webapp.dist\JobCrawlerUI.exe
rem
rem The dist folder is self-contained (bundled CPython + Flask + the crawler
rem package + lxml etc.) - copy it to any Windows machine and double-click
rem the exe; no Python or pip installs needed there. Data resolution:
rem JOBS_DATA_DIR env var if set; else the exe's folder when it already has
rem data; else the PARENT folder when it holds local_tech.db (dist inside
rem this repo -> uses the repo's real DB); else the exe's folder with a
rem fresh DB (copied to a new machine). Set ANTHROPIC_API_KEY in the
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
  --include-package=jobcrawler ^
  --include-data-files=webui/index.html=webui/index.html ^
  --include-data-files=profile.example.toml=profile.example.toml ^
  --nofollow-import-to=playwright ^
  --nofollow-import-to=playwright_stealth ^
  --assume-yes-for-downloads
if errorlevel 1 (
  echo.
  echo Build FAILED - see output above.
) else (
  echo.
  echo Build OK: webapp.dist\JobCrawlerUI.exe
)
pause
