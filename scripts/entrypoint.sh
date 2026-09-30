#!/bin/sh
set -e

if [ "$#" -gt 0 ]; then
    # Worker roles wait for the web service's migration; never race Alembic writers.
    exec "$@"
fi
echo "Running database migrations..."
alembic upgrade head
echo "Starting web application..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WEB_WORKERS:-1}"
