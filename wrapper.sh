#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/downloads/service-electric-epg"
source venv/bin/activate
# Ensure directory exists before logging
mkdir -p "$HOME/logs"
python scripts/fetch_epg.py >> "$HOME/logs/epg_fetch.log" 2>&1
