@echo off
setlocal
rem -P: the current directory never shadows the installed package.
rem A versioned install names its active release in current.txt (install.ps1 -Activate).
set "_HYDRAWEAVE_PY="
if exist "%~dp0current.txt" (
  set /p _HYDRAWEAVE_RELEASE=<"%~dp0current.txt"
  call set "_HYDRAWEAVE_PY=%~dp0venvs\%%_HYDRAWEAVE_RELEASE%%\Scripts\python.exe"
)
if not defined _HYDRAWEAVE_PY set "_HYDRAWEAVE_PY=%~dp0venv\Scripts\python.exe"
if not exist "%_HYDRAWEAVE_PY%" set "_HYDRAWEAVE_PY=%LOCALAPPDATA%\hydraweave\venv\Scripts\python.exe"
if not exist "%_HYDRAWEAVE_PY%" (
  echo Hydraweave is not installed. Run windows\install.ps1. 1>&2
  exit /b 1
)
"%_HYDRAWEAVE_PY%" -P -m hydraweave %*
exit /b %errorlevel%
