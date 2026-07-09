# Idempotency Improvements

This document describes the idempotency enhancements made to the service-electric-epg repository to ensure safe, repeatable execution.

## Overview

**Idempotency guarantees**: Running the EPG fetch multiple times in succession or concurrently produces the same results and never corrupts the live guide.

## Key Improvements

### 1. README Documentation

Added comprehensive idempotency documentation including:

- **Idempotency Guarantees** section explaining atomic writes, validation, concurrency control, change detection, and no-partial-updates
- **Testing Locally** section with examples of how to verify idempotent behavior
- **Error Handling and Recovery** section documenting failure modes and recovery
- **Troubleshooting** section with common issues and solutions
- Updated **Limitations** section to clarify rate limits and idempotent fetch behavior

### 2. Enhanced `wrapper.sh` Script

Improvements for local/Termux execution:

- **Better locking**: Uses file descriptor-based locking with proper cleanup
- **Comprehensive logging**: Dual logging to console and file with timestamps and severity levels
- **Error handling**: Validates all preconditions before starting the fetch
- **Configurability**: Environment variables for `REPO_ROOT`, `LOCKDIR`, `LOGDIR` to work in different environments
- **Robust variable handling**: Uses `set -a`/`set +a` for safe environment sourcing
- **PID tracking**: Stores PID for debugging stuck processes
- **Graceful concurrency**: Returns 0 (success) if another instance is running, allowing cron to proceed without errors

### 3. Improved `fetch_epg.py` Script

Enhancements for safe, validated output:

- **XML validation function** (`_validate_xml_file`): Parses the generated XML before moving it to the live location
- **Atomic file writes**: Writes to a temp file, validates, then atomically moves into place
- **Better logging**: Added startup logging showing parameters and fetch progress
- **Pre-validation check**: XML is validated before the atomic `replace()` operation, preventing corrupted files from reaching consumers
- **Updated docstring**: Clarifies the idempotency guarantees in the script

## Idempotency Guarantees

### 1. Atomic Writes
- Output files are written to a temporary file (`*.tmp`) first
- XML structure and content are validated
- Atomic `Path.replace()` moves the temp file into place
- A failed fetch never partially overwrites the guide

### 2. Validation Before Commit
- Generated XML is validated to be well-formed using `ElementTree.parse()`
- Invalid XML never reaches consumers
- Catches both API corruption and code bugs before deployment

### 3. Concurrency Control
- GitHub Actions: `concurrency.cancel-in-progress: false` ensures sequential execution
- Local `wrapper.sh`: File-level locking with `flock` prevents concurrent runs
- Other orchestrators: Use native job control to ensure single execution

### 4. Change Detection
- GitHub Actions workflow checks `git diff --cached --quiet`
- Only commits if XML content actually differs
- Minimizes noise in git history and avoids unnecessary rebuilds

### 5. No Partial Updates
- If any step fails (authentication, API fetch, validation, I/O), the previous valid guide remains untouched
- Failed runs log errors and exit with non-zero code
- Consumers always see the last known-good guide

## Testing Idempotency

To verify idempotent behavior locally:

```bash
# Run offline tests (no credentials needed)
python -m pytest tests/ -v

# Run the fetch script twice and compare outputs
export SD_USERNAME="..."
export SD_PASSWORD="..."
export SD_LINEUP_ID="..."
cd scripts
python fetch_epg.py  # First run
cp ../data/guide.xml ../data/guide.xml.run1
python fetch_epg.py  # Second run
diff ../data/guide.xml ../data/guide.xml.run1
# Should be identical except possibly whitespace
```

## Files Modified

1. **README.md**: Added idempotency documentation and troubleshooting
2. **wrapper.sh**: Improved locking, error handling, and configurability
3. **scripts/fetch_epg.py**: Added XML validation and better logging
4. **IDEMPOTENCY.md**: This file documenting all changes

## Backward Compatibility

All changes are fully backward compatible:
- Existing workflows and scripts continue to work unchanged
- New validation is an enhancement, not a breaking change
- Environment variable behavior is the same

## Recommendations for Users

1. If using GitHub Actions: No changes needed; idempotency is automatic
2. If using `wrapper.sh` on local/Termux:
   - Review the new script structure
   - Adjust `REPO_ROOT`, `LOCKDIR`, `LOGDIR` as needed for your environment
   - Check logs at `$LOGDIR/epg_fetch.log` for detailed execution history
3. If using a custom orchestrator:
   - Ensure only one fetch instance runs at a time
   - Monitor exit codes (0 = success, non-zero = failure)
   - Previous guides are preserved on failure
