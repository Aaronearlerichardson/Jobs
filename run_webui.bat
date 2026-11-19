@echo off
rem Launch the job-crawler web UI from source, in the `jobs` conda environment
rem (see envs/environment.yml). Opens your browser once the server is up. Safe
rem to double-click twice: a second launch just opens a tab to the running app.
rem
rem ASCII only - see the note in activate_env.bat.
cd /d "%~dp0"
call "%~dp0activate_env.bat"
if errorlevel 1 goto :end
python webapp.py --open
if errorlevel 1 (
  echo.
  echo If the error above is a missing module ^(e.g. flask^), refresh the env:
  echo   conda env update -f envs\environment.yml --prune
  echo If conda itself was not found, use the standalone build instead:
  echo run build_exe.bat once, then launch JobCrawlerUI.exe.
)
:end
pause
