@echo off
REM localai launcher. Resolves the venv relative to this file so the folder can move.
setlocal
set "LOCALAI_ROOT=%~dp0.."
set "LOCALAI_PY=%LOCALAI_ROOT%\.venv\Scripts\python.exe"
if not exist "%LOCALAI_PY%" (
  echo localai: virtualenv not found at "%LOCALAI_PY%" 1>&2
  echo Run install.ps1, or: python -m venv .venv ^&^& .venv\Scripts\python -m pip install -e . 1>&2
  exit /b 78
)
"%LOCALAI_PY%" -m localai %*
