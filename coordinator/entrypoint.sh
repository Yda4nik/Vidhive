#!/usr/bin/env bash
set -euo pipefail

# Apply database migrations, then start the API server.
echo "[coordinator] applying migrations..."
alembic upgrade head

echo "[coordinator] starting API on ${VIDHIVE_API_HOST:-0.0.0.0}:${VIDHIVE_API_PORT:-8000}"
exec uvicorn app.main:app \
    --host "${VIDHIVE_API_HOST:-0.0.0.0}" \
    --port "${VIDHIVE_API_PORT:-8000}"
