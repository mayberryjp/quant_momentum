#!/usr/bin/env bash
set -e

echo "Running database migrations..."
cd /app
alembic upgrade head
echo "Migrations complete."

exec supervisord -c /app/supervisord.conf
