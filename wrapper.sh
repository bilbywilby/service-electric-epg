#!/data/data/com.termux/files/usr/bin/bash
#
# Wrapper script for running the EPG fetch job with proper locking.
#
# This script ensures:
#  - Only one instance runs at a time (via flock)
#  - Environment variables are properly loaded and exported
#  - Comprehensive error handling and logging
#  - Idempotent behavior (safe to run multiple times)
#
# Usage: ./wrapper.sh
# Expected environment setup:
#  - Script located in the repo root
#  - config/.env file exists with credentials
#  - Python venv created in repo root

set -o pipefail

# Configuration
REPO_ROOT="${REPO_ROOT:-.}"
LOCKDIR="${LOCKDIR:-$HOME/run}"
LOGDIR="${LOGDIR:-$HOME/logs}"
LOCK_FILE="${LOCKDIR}/epg_fetch.lock"
LOG_FILE="${LOGDIR}/epg_fetch.log"
PID_FILE="${LOCKDIR}/epg_fetch.pid"

# Ensure directories exist
mkdir -p "$LOCKDIR" "$LOGDIR" || {
    echo "ERROR: Failed to create lock/log directories" >&2
    exit 2
}

# Logging function
log() {
    local level="$1"
    shift
    local msg="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] $msg" | tee -a "$LOG_FILE"
}

# Cleanup on exit
cleanup() {
    local exit_code=$?
    if [[ -n "$LOCK_FD" ]]; then
        exec {LOCK_FD}>&-  # Close the file descriptor
    fi
    rm -f "$LOCK_FILE"
    if [[ $exit_code -ne 0 ]]; then
        log "ERROR" "Script exited with code $exit_code"
    fi
    exit $exit_code
}

trap cleanup EXIT INT TERM

# Try to acquire exclusive lock (non-blocking)
exec {LOCK_FD}>"$LOCK_FILE" || {
    log "ERROR" "Failed to open lock file"
    exit 2
}

flock -n "$LOCK_FD" || {
    log "INFO" "Another instance is already running; exiting gracefully"
    exit 0
}

# Store PID for debugging
echo $$ > "$PID_FILE" 2>/dev/null || true

log "INFO" "Starting EPG fetch job (PID: $$)"

# Verify we're in the repo
if [[ ! -f "$REPO_ROOT/scripts/fetch_epg.py" ]]; then
    log "ERROR" "fetch_epg.py not found in $REPO_ROOT/scripts/"
    log "ERROR" "Please set REPO_ROOT or run from repo directory"
    exit 1
fi

# Load environment variables from config
ENV_FILE="$REPO_ROOT/config/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    log "ERROR" ".env file not found at $ENV_FILE"
    log "ERROR" "Copy config/settings.example.env to config/.env and fill in your credentials"
    exit 1
fi

# Source .env safely (only export variable assignments)
set -a
# shellcheck disable=SC1090
if ! source "$ENV_FILE"; then
    log "ERROR" "Failed to source $ENV_FILE"
    exit 1
fi
set +a

# Verify required variables are set
for var in SD_USERNAME SD_PASSWORD SD_LINEUP_ID; do
    if [[ -z "${!var}" ]]; then
        log "ERROR" "Required variable $var is not set in $ENV_FILE"
        exit 1
    fi
done

# Set defaults for optional variables
OUTPUT_PATH="${OUTPUT_PATH:-$REPO_ROOT/data/guide.xml}"
DAYS_AHEAD="${DAYS_AHEAD:-10}"
SD_USER_AGENT="${SD_USER_AGENT:-ServiceElectricEPG/1.0}"

# Export for Python script
export SD_USERNAME
export SD_PASSWORD
export SD_LINEUP_ID
export DAYS_AHEAD
export OUTPUT_PATH
export SD_USER_AGENT

# Verify Python environment
if [[ ! -f "$REPO_ROOT/venv/bin/python" ]]; then
    log "ERROR" "Python venv not found at $REPO_ROOT/venv"
    log "ERROR" "Create it with: python3 -m venv $REPO_ROOT/venv"
    exit 1
fi

# Source venv
# shellcheck disable=SC1091
if ! source "$REPO_ROOT/venv/bin/activate"; then
    log "ERROR" "Failed to activate venv"
    exit 1
fi

# Run the fetch script
log "INFO" "Executing fetch_epg.py"
cd "$REPO_ROOT/scripts" || {
    log "ERROR" "Failed to change to scripts directory"
    exit 1
}

if python fetch_epg.py; then
    log "INFO" "Fetch completed successfully"
    exit 0
else
    EXIT_CODE=$?
    log "ERROR" "Fetch failed with exit code $EXIT_CODE"
    exit $EXIT_CODE
fi
