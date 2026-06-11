#!/bin/bash
# Azure App Service startup script
# Set in: Configuration → General Settings → Startup Command:
#   bash startup.sh
pip install -r requirements.txt
# IMPORTANT: --workers MUST stay 1.
# All game state lives in process memory (STATE dict). With 2+ workers each
# process holds its own divergent copy and the periodic db_save() snapshots
# overwrite each other ("last writer wins"), randomly losing submitted answers
# and scores mid-exercise. One uvicorn worker comfortably handles a tabletop
# exercise (50+ participants polling every 3s).
gunicorn main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:${WEBSITES_PORT:-8000} --timeout 120 --keep-alive 5
