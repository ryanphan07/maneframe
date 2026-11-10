@echo off
rem Double-click to start Maneframe. Uses only the files in this folder (no Python, Node, ffmpeg or git needed).
setlocal
cd /d "%~dp0"
title Maneframe
"%~dp0python\python.exe" -u "%~dp0launcher.py"
if errorlevel 1 (
  echo.
  echo Maneframe did not start. Read the message above.
  pause
)
