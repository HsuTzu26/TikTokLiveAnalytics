@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m src.chat_sender
pause
