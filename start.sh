#!/bin/bash

# ensure the db file exists (volume mount creates a directory if file is missing)
touch /app/namenu.db

# run scraper immediately on startup so DB isn't empty on first boot
python main.py --add-key "admin"
bash scrapeAll.sh

# write the cron job to a file
echo "0 6,12,18,0 * * * cd /app && bash scrapeAll.sh >> /var/log/scraper.log 2>&1" > /etc/cron.d/scraper

# give it correct permissions (cron is picky about this)
chmod 0644 /etc/cron.d/scraper
crontab /etc/cron.d/scraper

# start cron in background
cron

# start API in foreground — this keeps the container alive
uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}