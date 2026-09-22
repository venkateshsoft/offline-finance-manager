@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
pause
