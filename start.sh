#!/bin/bash
# Z API 启动脚本
cd "$(dirname "$0")"
PORT=$(python3 -c "from app.config import settings; print(settings.SERVER_PORT)")
exec python3 -c "import uvicorn; uvicorn.run('app.main:app', host='0.0.0.0', port=$PORT)"
