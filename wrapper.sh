#!/data/data/com.termux/files/usr/bin/bash

# Define lock file and log file locations
LOCKFILE="$HOME/logs/epg_fetch.lock"
LOGFILE="$HOME/logs/epg_fetch.log"

# Ensure log directory exists
mkdir -p "$HOME/logs"

# Use flock to ensure only one instance runs.
(
  # Try to acquire lock immediately. If busy, log and exit.
  flock -n 200 || { echo "$(date): Job already running, skipping." >> "$LOGFILE"; exit 0; }

  cd "$HOME/downloads/service-electric-epg" || { echo "$(date): CD failed" >> "$LOGFILE"; exit 1; }
  
  # Load environment variables
  if [ -f config/.env ]; then
      # Source the file
      . config/.env
      # Explicitly export the critical variables to ensure Python sees them
      export SD_USERNAME
      export SD_PASSWORD
      export SD_LINEUP_ID
      export DAYS_AHEAD
      export OUTPUT_PATH
      export SD_USER_AGENT
  else
      echo "$(date): ERROR: config/.env not found" >> "$LOGFILE"
      exit 1
  fi

  # Activate virtual environment
  if [ -f venv/bin/activate ]; then
      . venv/bin/activate
  else
      echo "$(date): ERROR: venv not found" >> "$LOGFILE"
      exit 1
  fi
  
  # Execute the fetch script
  echo "$(date): Starting EPG fetch job" >> "$LOGFILE"
  python scripts/fetch_epg.py >> "$LOGFILE" 2>&1
  EXIT_CODE=$?
  echo "$(date): EPG fetch job completed with exit code $EXIT_CODE" >> "$LOGFILE"

) 200>"$LOCKFILE"
