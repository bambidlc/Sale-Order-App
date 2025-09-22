@echo off
echo Starting API on http://127.0.0.1:8000
uvicorn app:app --reload
pause
