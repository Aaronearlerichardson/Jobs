@echo off
rem Locate conda and activate the environment the crawler runs in. Called by
rem run_webui.bat / build_exe.bat, not meant to be run on its own. On success
rem `python` is the env's interpreter for the rest of the calling script; on
rem failure it prints why and returns a non-zero errorlevel.
rem
rem Set JOBS_ENV before launching to use a different environment name.
rem
rem ASCII only, deliberately: cmd.exe seeks this file by byte offset between
rem commands, so a multi-byte character (even inside a rem) desynchronises the
rem read and it starts executing the middle of a line.

if not defined JOBS_ENV set "JOBS_ENV=jobs"
set "CONDA_BAT="

rem An already-initialised shell exports CONDA_EXE as <root>\Scripts\conda.exe.
if defined CONDA_EXE for %%I in ("%CONDA_EXE%\..\..\condabin\conda.bat") do call :pick "%%~fI"

rem Otherwise the usual install roots, per-user first, then whatever is on PATH.
call :pick "%USERPROFILE%\miniconda3\condabin\conda.bat"
call :pick "%USERPROFILE%\anaconda3\condabin\conda.bat"
call :pick "%USERPROFILE%\miniforge3\condabin\conda.bat"
call :pick "%LOCALAPPDATA%\miniconda3\condabin\conda.bat"
call :pick "%ProgramData%\miniconda3\condabin\conda.bat"
call :pick "%ProgramData%\anaconda3\condabin\conda.bat"
for /f "delims=" %%I in ('where conda.bat 2^>nul') do call :pick "%%~I"

if not defined CONDA_BAT (
  echo.
  echo   [!] conda was not found. Install Miniconda, or point activate_env.bat
  echo       at your install if it lives somewhere unusual.
  exit /b 1
)

call "%CONDA_BAT%" activate "%JOBS_ENV%"
if errorlevel 1 (
  echo.
  echo   [!] could not activate the "%JOBS_ENV%" conda environment.
  echo       Create it once with:  conda env create -f envs\environment.yml
  exit /b 1
)
exit /b 0

:pick
if not defined CONDA_BAT if exist %1 set "CONDA_BAT=%~1"
exit /b 0
