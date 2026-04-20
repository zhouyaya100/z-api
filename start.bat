@echo off
cd /d %~dp0
python -c "import uvicorn; from app.config import settings; uvicorn.run('app.main:app', host=settings.SERVER_HOST, port=settings.SERVER_PORT, workers=settings.SERVER_WORKERS)"
pause
