@echo off
setlocal

REM Load env from .env if present
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

python bot.py

endlocal
