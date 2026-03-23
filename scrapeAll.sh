#!/bin/bash
# scrapeAll.sh — runs all scrapers in sequence
set -e
cd "$(dirname "$0")"
ARGS="$@"
echo "=============================="
echo " ToMenu scrapeAll $(date '+%Y-%m-%d %H:%M:%S')"
echo " args: ${ARGS:-'(full week)'}"
echo "=============================="
python -X utf8 scrapers/namenu.scrape.py $ARGS
echo "=============================="
echo " all scrapers done"
echo "=============================="