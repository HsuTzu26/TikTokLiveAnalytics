@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Create the project environment first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -X utf8 -m streamlit run app\gift_catalog_editor.py --server.address 127.0.0.1 --server.port 8503
