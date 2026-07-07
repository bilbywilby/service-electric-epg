#!/data/data/com.termux/files/usr/bin/bash

# Define lock file and log file locations
LOCKFILE="$HOME/logs/epg_fetch.lock"
LOGFILE="$HOME/logs/epg_fetch.log"

# Ensure log directory exists
mkdir -p "$HOME/logs"

# Use flock to ensure only one instance runs.
# File descriptor 200 is used for the lock.
# -n flag causes flock to exit immediately if the lock is held.
(
  flock -n 200 || { echo "$(date): Job already running, skipping." >> "$LOGFILE"; exit 0; }

  cd "$HOME/downloads/service-electric-epg" || exit 1
  
  # Activate virtual environment
  source venv/bin/activate
  
  # Execute the fetch script with logging
  echo "$(date): Starting EPG fetch job" >> "$LOGFILE"
  python scripts/fetch_epg.py >> "$LOGFILE" 2>&1
  echo "$(date): EPG fetch job completed" >> "$LOGFILE"

) 200>"$LOCKFILE"
