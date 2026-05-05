#!/bin/bash
# Azure App Service startup script
# Set in: Configuration → General Settings → Startup Command:
#   bash startup.sh
pip install -r requirements.txt
gunicorn main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:${WEBSITES_PORT:-8000} --timeout 120 --keep-alive 5
