@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ensimmainen kaynnistys: asennetaan riippuvuudet...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
