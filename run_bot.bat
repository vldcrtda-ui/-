@echo off
setlocal

REM Ensure we run from the directory where the script lives
cd /d "%~dp0"

REM Load env from .env if present
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

python bot.py

endlocal
