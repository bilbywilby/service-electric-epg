#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/downloads/service-electric-epg"
source venv/bin/activate
python scripts/fetch_epg.py
