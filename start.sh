#!/bin/bash
# start.sh
# Container entrypoint — launches restaurant_api.py and api.py.
# restaurant_api.py is the local stand-in for restaurant.tomenu.sk.
# When RESTAURANT_API_URL is set to an external URL, restaurant_api.py
# is still started but the main backend will ignore it.

set -e

echo "Starting ToMenu on port ${PORT:-8000} / restaurant API on port ${RESTAURANT_API_PORT:-6333}"

mkdir -p /app/data
touch /app/data/main.db
touch /app/data/namenu.db

# Create admin key on first boot (skips if already exists)
KEY_OUTPUT=$(python main.py --add-key "admin")
echo "$KEY_OUTPUT"

# Initial scrape so the DB is never empty on first boot
bash scrapeAll.sh

# Cron for scheduled scrapes
echo "0 6,12,18,0 * * * cd /app && bash scrapeAll.sh >> /var/log/scraper.log 2>&1" > /etc/cron.d/scraper
chmod 0644 /etc/cron.d/scraper
crontab /etc/cron.d/scraper
cron

echo ""
echo "=============================="
echo " ToMenu ready"
echo "=============================="
echo "$KEY_OUTPUT"
echo "=============================="
echo ""

# Start restaurant API in background
uvicorn restaurant_api:app \
  --host 0.0.0.0 \
  --port "${RESTAURANT_API_PORT:-6333}" \
  --no-access-log &

RESTAURANT_PID=$!
echo "restaurant_api running (pid $RESTAURANT_PID) on port ${RESTAURANT_API_PORT:-6333}"

# Start main API in foreground — keeps container alive
uvicorn api:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}"

# If main API exits, kill restaurant API too
kill $RESTAURANT_PID 2>/dev/null || true