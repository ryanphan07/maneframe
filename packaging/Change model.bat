@echo off
rem Double-click to pick a different language model. Downloaded models are kept; Maneframe starts afterwards as usual.
setlocal
cd /d "%~dp0"
title Maneframe
"%~dp0python\python.exe" -u "%~dp0launcher.py" --change-model
if errorlevel 1 (
  echo.
  echo Maneframe did not start. Read the message above.
  pause
)
