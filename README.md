# service-electric-epg

Generates a daily XMLTV guide (`data/guide.xml`) for Service Electric Cable
TV & Communications (Lehigh Valley / Lehigh County, PA) and commits it to
this repo, so any XMLTV consumer -- Telly Skout, Tvheadend, TVheadend-based
DVRs, Jellyfin, etc. -- can point at the raw GitHub URL and always see a
current guide with no manual intervention.

## What this actually is

There is no free, public EPG feed for a specific regional cable provider's
exact channel lineup. This pulls real schedule data from
[Schedules Direct](https://www.schedulesdirect.org/), the non-profit,
open-source-focused successor to the old Zap2it/tv_grab_zap2it grabber (that
free Zap2it access was shut off in 2015 -- if you found older instructions
referencing it, they're dead). Schedules Direct requires a paid account
(currently **US$35/year**, with a 7-day free trial and a $9 two-month option)
and is licensed for individual, non-commercial use with open-source software,
which is exactly this repo's use case.

## How it works

1. `.github/workflows/update-guide.yml` runs once a day (and on manual
   trigger) on GitHub's infrastructure -- nothing needs to stay running on
   your machine or a Termux session.
2. `scripts/fetch_epg.py` authenticates to the Schedules Direct JSON API,
   pulls your lineup's channel map, a rolling window of schedule data, and
   the associated program metadata, and writes `data/guide.xml`.
3. A test job (`tests/test_build_xmltv.py`) runs first on every trigger; if
   it fails, the fetch-and-commit job never runs, so a bad code change can't
   silently corrupt the guide.
4. The workflow only commits if the generated file actually differs from
   what's already in the repo, and only after confirming the output parses
   as well-formed XML -- a failed API call produces a non-zero exit and
   **no commit**, so consumers always see the last good guide instead of an
   empty or truncated one.

## Idempotency guarantees

This service is designed to be **fully idempotent** — it is safe to run
multiple times concurrently or in close succession with the following
guarantees:

- **Atomic writes**: Output files are written to a temporary file first, then
  atomically moved into place. A failed fetch never overwrites a valid guide.
- **Validation before commit**: XML is validated to be well-formed before
  any file is written. Invalid data never reaches consumers.
- **Concurrency control**: In GitHub Actions, `concurrency.cancel-in-progress: false`
  ensures sequential execution — only one workflow runs at a time.
- **Change detection**: Commits only happen if the generated XML differs
  from what's already in the repo, minimizing unnecessary commits.
- **No partial updates**: If any step fails (authentication, API fetch,
  validation, or file I/O), the previous valid guide remains untouched.

If you run this locally using `wrapper.sh`, file-level locking (`flock`)
prevents concurrent executions. If you run the fetch script directly
(e.g., in a container or scheduled task), ensure only one instance
executes at a time using your orchestrator's native locking or job control.

## One-time setup

### 1. Create a Schedules Direct account
Sign up at schedulesdirect.org. You'll authenticate with this account's
email and password (the password is SHA1-hashed locally before ever being
sent -- see `scripts/sdclient.py`).

### 2. Find your Service Electric lineup ID
Lineup IDs are account- and zip-code-specific, so there's no way to hardcode
one correctly for you. Run the discovery helper locally (Termux is fine):

```bash
git clone <this-repo-url>
cd service-electric-epg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/discover_lineup.py --postal-code 18101
```

Use a zip code inside Service Electric's Lehigh Valley footprint --
Allentown (181xx), Bethlehem (180xx), Easton (180xx/18042/18045), or Emmaus
(18049) are good starting points. The script lists every lineup whose name
contains "service electric" for that zip, lets you pick one interactively,
and adds it to your SD account. It prints the lineup ID
(e.g. `USA-PA12345-X`) -- save it.

If nothing matches, it prints every lineup found for that zip so you can
identify the right one by eye and re-run with `--add <lineup-id>` directly.

### 3. Configure the GitHub repository
In **Settings > Secrets and variables > Actions**:

| Name | Type | Value |
|---|---|---|
| `SD_USERNAME` | Secret | Your Schedules Direct account email |
| `SD_PASSWORD` | Secret | Your Schedules Direct account password |
| `SD_LINEUP_ID` | Variable | The lineup ID from step 2 |
| `DAYS_AHEAD` | Variable (optional) | Days of schedule to fetch, default `10` |

Secrets are encrypted and never shown in logs; Variables are plain but not
secret, which is appropriate for the lineup ID since it isn't credentials.

### 4. Trigger the first run
Push this repo, then either wait for the next scheduled run or trigger it
manually from the **Actions** tab (`Update XMLTV Guide` > `Run workflow`).

## Testing locally

To verify setup and test the fetch without waiting for the scheduled cron:

```bash
# Run offline tests (no network, no credentials needed)
python -m pytest tests/ -v

# Run the actual fetch (requires SD_* env vars set)
cd scripts
export SD_USERNAME="your-email@example.com"
export SD_PASSWORD="your-password"
export SD_LINEUP_ID="USA-PA12345-X"
export OUTPUT_PATH="../data/guide.xml"
python fetch_epg.py
```

If run multiple times in succession, the script will produce identical
output (same XML structure and data) because it fetches the same schedule
window and builds the same guide. Only the generation timestamp in logs differs.

## Error handling and recovery

**If a fetch fails:**
- The script logs the error and exits with a non-zero code
- No file is written; the previous `data/guide.xml` remains untouched
- In GitHub Actions, a failed fetch prevents the commit step
- Consumers continue seeing the last known-good guide

**If you see validation errors:**
- The XML is checked to be well-formed before any commit
- If parsing fails, you'll see an error and the old guide remains in place
- This catches both API corruption and code bugs

**If a run is interrupted mid-fetch:**
- The temporary file (`*.tmp`) is abandoned
- Your main guide remains valid
- The next run will attempt the fetch again

## Pointing your XMLTV consumer at the guide

Once the first commit lands, the raw file is at:

```
https://raw.githubusercontent.com/<your-username>/<repo-name>/main/data/guide.xml
```

Put that URL in the "File:" field of your XMLTV fetcher settings (this is
exactly the field visible in Telly Skout's Settings screen).

## Troubleshooting

### "Job already running, skipping"
This is expected behavior with `wrapper.sh` on local/Termux setups.
If you see this repeatedly, a previous run may be stuck. Check:
```bash
ps aux | grep fetch_epg
```
If hung, kill it and the next cron run will proceed. GitHub Actions handles
this automatically.

### Guide hasn't updated in days
- Check **Actions** tab for workflow errors or timeouts
- Verify secrets are set (Settings > Secrets and variables > Actions)
- Verify the lineup ID is correct (try rediscovery with `scripts/discover_lineup.py`)
- Check Schedules Direct status page (schedulesdirect.org) for service issues

### XML parsing errors on update
This usually means the Schedules Direct API returned corrupt data, or a bug
was introduced. The workflow tests run first; if they fail, no fetch happens.
Check the test job's output for details.

### Running multiple times produces different guides
This should not happen. If it does:
1. Verify the same `DAYS_AHEAD` value is being used
2. Check that each run has fresh Schedules Direct credentials (tokens are cached)
3. If one run is concurrent with another, file-locking should prevent corruption,
   but increase delays. Check wrapper.sh logs.

### My guide won't update because the content hasn't changed
This is correct behavior and a feature, not a bug. If Schedules Direct returns
the same data (no new programs, no schedule changes), the workflow detects no
differences and skips the commit to keep the repo clean and the git history
readable.

## Honest limitations

- **This costs money.** Schedules Direct is not free after the trial. There
  is no legitimate free alternative with this level of accuracy for a named
  regional MSO's exact lineup.
- **Cron drift across DST.** GitHub Actions cron schedules run in fixed UTC
  and don't shift for Daylight Saving Time, so the wall-clock run time in
  Lehigh County will drift by an hour twice a year unless you adjust the
  cron expression in `update-guide.yml` yourself in March/November.
- **Schedules Direct rate limits.** Per their documented policy: a maximum
  of 4 lineups per account by default, at most 6 lineup *additions* per
  24 hours, and a 24-hour token lifetime that `sdclient.py` caches and
  reuses rather than re-requesting. A single daily run is nowhere near
  these limits; hammering the discovery script repeatedly could hit them.
  The fetch script itself is safe to run repeatedly within the same day
  (it re-authenticates and fetches fresh data, and is fully idempotent).
- **Licensing.** Schedules Direct's data license restricts use to individual,
  non-commercial, open-source applications -- this repo's daily-cron,
  single-account setup fits that, but redistributing the generated
  `guide.xml` publicly at scale or building a commercial product on top of
  it would not.
- **"Fuzzier" far-future data.** Schedules Direct's own guidance is that
  schedule accuracy degrades the further out you query; the default
  `DAYS_AHEAD=10` stays comfortably inside their more reliable window
  (they publish up to ~20 days for US lineups).

## Repository layout

```
.github/workflows/update-guide.yml   Daily cron + manual trigger
scripts/sdclient.py                  Typed Schedules Direct JSON API client
scripts/discover_lineup.py           One-time interactive lineup finder (run locally)
scripts/fetch_epg.py                 Daily fetch + XMLTV writer (run by the workflow)
tests/test_build_xmltv.py            Offline pytest smoke tests, no credentials needed
config/settings.example.env          Template for local runs
data/guide.xml                       Generated output (created after first successful run)
```
