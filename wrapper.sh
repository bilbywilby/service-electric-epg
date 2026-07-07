#!/data/data/com.termux/files/usr/bin/bash

# Define lock file and log file locations
LOCKFILE="$HOME/logs/epg_fetch.lock"
LOGFILE="$HOME/logs/epg_fetch.log"

# Ensure log directory exists
mkdir -p "$HOME/logs"

# Use flock to ensure only one instance runs.
(
  flock -n 200 || { echo "$(date): Job already running, skipping." >> "$LOGFILE"; exit 0; }

  cd "$HOME/downloads/service-electric-epg" || exit 1
  
  # Load environment variables from config/.env
  if [ -f config/.env ]; then
      # 'set -a' automatically exports all variables defined in the sourced file
      set -a
      source config/.env
      set +a
  else
      echo "$(date): ERROR: config/.env not found. Cannot start." >> "$LOGFILE"
      exit 1
  fi

  # Activate virtual environment
  source venv/bin/activate
  
  # Execute the fetch script with logging
  echo "$(date): Starting EPG fetch job" >> "$LOGFILE"
  python scripts/fetch_epg.py >> "$LOGFILE" 2>&1
  echo "$(date): EPG fetch job completed" >> "$LOGFILE"

) 200>"$LOCKFILE"
