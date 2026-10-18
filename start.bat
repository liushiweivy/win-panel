@echo off
rem win-panel launcher - localhost only (127.0.0.1:8830)
rem Pure ASCII; python-path.txt (gitignored) can override the interpreter.
cd /d "%~dp0"
set PY=python
if exist "%~dp0python-path.txt" set /p PY=<"%~dp0python-path.txt"
"%PY%" app.py
pause
