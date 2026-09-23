@echo off
setlocal
if not defined AUTOPOSTER_WEB_PASSWORD (
  set /p AUTOPOSTER_WEB_PASSWORD=Enter web admin password:
)
if not defined AUTOPOSTER_WEB_PASSWORD (
  echo Password is required.
  exit /b 1
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" web_main.py
) else (
  python web_main.py
)
