#!/usr/bin/env python3
"""
EPG Generator: Fetches schedule data from Schedules Direct and builds an XMLTV file.

This script is fully idempotent:
  - Fetches from Schedules Direct API
  - Builds XMLTV ElementTree and validates structure
  - Writes atomically to a temp file, then moves into place
  - On any error, previous valid guide remains untouched
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

try:
    from .sdclient import SDClient, SDError, Station
except ImportError:
    try:
        from sdclient import SDClient, SDError, Station
    except ImportError:
        print("Error: could not import sdclient.py. Run this from the scripts/ directory, "
              "or ensure scripts/ is on PYTHONPATH. ('pip install requests' if that's the underlying error.)")
        sys.exit(1)

DEFAULT_USER_AGENT = "ServiceElectricEPG/1.0 (Automated EPG Generator)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    """Get environment variable, raise error if required and missing."""
    value = os.getenv(key, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    lineup_id: str
    days_ahead: int
    output_path: Path
    user_agent: str


# SD lineup IDs are documented as COUNTRY-LOCATION-DEVICE, e.g. "USA-NY12345-X"
# or "USA-OTA12345-X" for over-the-air. Deliberately loose (this isn't SD's
# own validator, just a sanity check to catch an obviously wrong value --
# a pasted username, an empty string that slipped past _env, etc. -- before
# spending a network round-trip finding out).
_LINEUP_ID_RE = re.compile(r"^[A-Z]{3}-[A-Za-z0-9]+-[A-Za-z0-9]+$")


def _load_config() -> Config:
    """
    Read and validate all startup configuration in one place, so a bad or
    missing value fails immediately with one clear message instead of
    surfacing several authenticated requests later as a confusing SD API
    error or a raw stack trace.
    """
    username = _env("SD_USERNAME", required=True)
    password = _env("SD_PASSWORD", required=True)
    lineup_id = _env("SD_LINEUP_ID", required=True)
    days_ahead_raw = _env("DAYS_AHEAD", "10")
    output_path_raw = _env("OUTPUT_PATH", "data/guide.xml")
    user_agent = _env("SD_USER_AGENT", DEFAULT_USER_AGENT)

    problems: list[str] = []

    if "@" not in username:
        problems.append(f"SD_USERNAME does not look like an email address: {username!r}")

    if not _LINEUP_ID_RE.match(lineup_id):
        problems.append(
            f"SD_LINEUP_ID {lineup_id!r} doesn't match the expected COUNTRY-LOCATION-DEVICE "
            "shape (e.g. 'USA-NY12345-X'). Run scripts/discover_lineup.py to find the correct value."
        )

    try:
        days_ahead = int(days_ahead_raw)
        if days_ahead < 1:
            problems.append(f"DAYS_AHEAD must be >= 1, got {days_ahead}")
    except ValueError:
        problems.append(f"DAYS_AHEAD must be an integer, got {days_ahead_raw!r}")
        days_ahead = 0  # placeholder; we're failing out below regardless

    output_path = Path(output_path_raw)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        probe = output_path.parent / f".write-probe-{os.getpid()}"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        problems.append(f"OUTPUT_PATH's directory ({output_path.parent}) isn't writable: {exc}")

    if problems:
        raise ValueError(
            "Invalid configuration, refusing to start:\n" + "\n".join(f"  - {p}" for p in problems)
        )

    return Config(
        username=username,
        password=password,
        lineup_id=lineup_id,
        days_ahead=days_ahead,
        output_path=output_path,
        user_agent=user_agent,
    )


def _daterange(days_ahead: int) -> list[str]:
    """
    Build the list of calendar-date strings ("YYYY-MM-DD") to request from
    Schedules Direct's POST /schedules endpoint. SD buckets schedule data
    by calendar date, not by timestamp range, so this returns `days_ahead`
    consecutive dates starting today. Anchored to UTC (not local time) so
    behavior is identical whether this runs on a UTC GitHub Actions runner
    or a Termux device in any timezone -- consistent with airDateTime
    always being in "Z" time per the SD API spec.
    """
    if days_ahead < 1:
        raise ValueError(f"days_ahead must be >= 1, got {days_ahead}")
    today = datetime.now(timezone.utc).date()
    return [(today + timedelta(days=offset)).isoformat() for offset in range(days_ahead)]


def _validate_xml_file(file_path: Path) -> bool:
    """
    Validate that a file is well-formed XML without corruption.
    Returns True if valid, False otherwise.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            ET.parse(fh)
        return True
    except ET.ParseError as e:
        logger.error("XML validation failed: %s", e)
        return False
    except Exception as e:
        logger.error("Unexpected error validating XML: %s", e)
        return False


def _validate_xmltv_structure(file_path: Path) -> bool:
    """
    Validate XMLTV-specific structural invariants that well-formedness alone
    doesn't catch. This is deliberately narrower than full DTD validation
    (no external DTD fetch, no xmltv.dtd dependency at runtime) but catches
    the corruption patterns that actually matter to downstream consumers
    like Plex, Jellyfin, and TVHeadend:

      - root element is <tv>
      - every <channel> has an 'id' attribute, and no two channels share one
      - every <programme> has a 'channel' attribute
      - every <programme>'s channel attribute references a <channel id=...>
        that actually exists in the same document
      - every <programme> has at least one <title> child

    Returns True if the document satisfies all of the above, logging the
    first problem found and returning False otherwise.
    """
    try:
        tree = ET.parse(file_path)
    except ET.ParseError as e:
        logger.error("XML structural validation failed: could not parse: %s", e)
        return False

    root = tree.getroot()
    if root.tag != "tv":
        logger.error("XML structural validation failed: root element is <%s>, expected <tv>", root.tag)
        return False

    channel_ids: set[str] = set()
    for channel_el in root.findall("channel"):
        cid = channel_el.get("id")
        if not cid:
            logger.error("XML structural validation failed: a <channel> element has no 'id' attribute")
            return False
        if cid in channel_ids:
            logger.error("XML structural validation failed: duplicate channel id %r", cid)
            return False
        channel_ids.add(cid)

    for programme_el in root.findall("programme"):
        channel_ref = programme_el.get("channel")
        if not channel_ref:
            logger.error("XML structural validation failed: a <programme> element has no 'channel' attribute")
            return False
        if channel_ref not in channel_ids:
            logger.error(
                "XML structural validation failed: <programme channel=%r> references a channel "
                "that has no matching <channel id=%r> element",
                channel_ref, channel_ref,
            )
            return False
        if programme_el.find("title") is None:
            logger.error(
                "XML structural validation failed: <programme channel=%r start=%r> has no <title>",
                channel_ref, programme_el.get("start"),
            )
            return False

    return True


def build_xmltv(stations: list[Station], schedules: list[dict], programs: dict[str, Any]) -> ET.Element:
    """Build the XMLTV ElementTree from fetched data."""
    tv_el = ET.Element("tv")
    tv_el.set("source-info-url", "https://www.schedulesdirect.org/")
    tv_el.set("source-info-name", "Schedules Direct")
    tv_el.set("generator-info-name", "ServiceElectricEPG")
    tv_el.set("generator-info-url", "https://github.com/bilbywilby/service-electric-epg")

    for station in stations:
        channel_el = ET.SubElement(tv_el, "channel", id=station.station_id)
        if station.channel:
            ET.SubElement(channel_el, "display-name").text = f"{station.channel} {station.callsign or station.station_id}"
        ET.SubElement(channel_el, "display-name").text = station.name or station.callsign or station.station_id
        if station.icon_url:
            ET.SubElement(channel_el, "icon", src=station.icon_url)

    programme_count = 0
    skipped_count = 0
    duplicate_count = 0
    seen_keys: set[tuple[str, str, str]] = set()

    # sdclient.get_schedules() returns one dict per requested station, each
    # shaped {"stationID": ..., "programs": [airing, airing, ...]} per the
    # documented POST /schedules response.
    all_airings = []
    if isinstance(schedules, list):
        for entry in schedules:
            if isinstance(entry, dict) and "programs" in entry:
                station_id = entry.get("stationID")
                for prog_entry in entry["programs"]:
                    prog_entry["stationID"] = station_id
                    all_airings.append(prog_entry)
            elif isinstance(entry, dict) and "programID" in entry:
                all_airings.append(entry)

    for airing in all_airings:
        program_id = airing.get("programID")
        station_id = airing.get("stationID")
        start_time = airing.get("airDateTime")
        duration = airing.get("duration", 0)  # SD API: integer seconds

        if not program_id or not start_time or not station_id:
            skipped_count += 1
            continue

        # SD's own docs describe `duration` as always present and numeric,
        # but a malformed or truncated response entry could still hand us a
        # string, None, or a negative number here. `int/float + non-numeric`
        # raises TypeError (not ValueError), which the parsing try/except
        # below doesn't catch, so this has to be checked before that block
        # or one bad airing takes down the whole build.
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            logger.warning(
                "Skipping programID=%s stationID=%s: invalid duration %r",
                program_id, station_id, duration,
            )
            skipped_count += 1
            continue

        program_data = programs.get(program_id)
        if not program_data:
            skipped_count += 1
            continue

        try:
            dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            start_str = dt_start.strftime("%Y%m%d%H%M%S ")
            start_str += "+0000"

            dt_end = dt_start + timedelta(seconds=duration)
            stop_str = dt_end.strftime("%Y%m%d%H%M%S +0000")
        except ValueError:
            skipped_count += 1
            continue

        if dt_end <= dt_start:
            logger.warning(
                "Skipping programID=%s stationID=%s: computed stop (%s) does not follow start (%s)",
                program_id, station_id, stop_str, start_str,
            )
            skipped_count += 1
            continue

        titles = program_data.get("titles", [])
        title = titles[0].get("title120", "Unknown Title") if titles else "Unknown Title"

        # SD feeds occasionally repeat the same airing (retransmission of a
        # schedule chunk, overlapping date-range requests, etc.). Same
        # channel + same start time + same title is treated as the same
        # broadcast; keep the first occurrence and drop the rest rather than
        # emitting duplicate <programme> blocks, which some EPG consumers
        # (Plex, TVHeadend) handle by simply showing both.
        dedup_key = (station_id, start_str, title)
        if dedup_key in seen_keys:
            duplicate_count += 1
            continue
        seen_keys.add(dedup_key)

        programme_el = ET.SubElement(tv_el, "programme", start=start_str, stop=stop_str, channel=station_id)
        ET.SubElement(programme_el, "title", lang="en").text = title

        if program_data.get("episodeTitle150"):
            ET.SubElement(programme_el, "sub-title", lang="en").text = program_data["episodeTitle150"]

        descriptions = program_data.get("descriptions", {})
        desc_list = descriptions.get("description1000") or descriptions.get("description100") or []
        if desc_list:
            ET.SubElement(programme_el, "desc", lang="en").text = desc_list[0].get("description", "")

        for genre in program_data.get("genres", []):
            ET.SubElement(programme_el, "category", lang="en").text = genre

        # XMLTV's DTD has no `category` *attribute* on <programme> -- the
        # ATTLIST only defines start/stop/pdc-start/vps-start/showview/
        # videoplus/channel/clumpidx. A show-type category belongs as
        # another <category> child element, same as the genre-derived ones
        # above, not as programme_el.set("category", ...).
        if program_data.get("showType"):
            st = program_data["showType"].lower()
            if "movie" in st:
                ET.SubElement(programme_el, "category", lang="en").text = "Movie"
            elif "series" in st:
                ET.SubElement(programme_el, "category", lang="en").text = "Series"

        if program_data.get("originalAirDate"):
            ET.SubElement(programme_el, "date").text = program_data["originalAirDate"].replace("-", "")

        content_ratings = program_data.get("contentRating", [])
        if content_ratings:
            r = next((x for x in content_ratings if x.get("country") == "USA"), content_ratings[0])
            rating_el = ET.SubElement(programme_el, "rating", system="MPAA")
            ET.SubElement(rating_el, "value").text = r.get("code", "")

        for meta_entry in program_data.get("metadata", []):
            season_episode = next(iter(meta_entry.values()), {})
            sea_num = season_episode.get("season")
            ep_num = season_episode.get("episode")
            # `is not None`, not truthiness: season/episode 0 is a real
            # value for specials in some catalogs, and `if sea_num and
            # ep_num` silently dropped both tags for those.
            if sea_num is not None and ep_num is not None:
                # xmltv_ns is zero-indexed *relative to 1-based broadcast
                # numbering* (season 1 -> "0", episode 1 -> "0"). Source
                # season/episode 0 (SD/Gracenote's convention for
                # specials) isn't part of that 1-based sequence, so
                # subtracting 1 from it would produce a nonsensical
                # negative index. Only emit xmltv_ns when both are >= 1;
                # "onscreen" has no such constraint and is always safe.
                if sea_num >= 1 and ep_num >= 1:
                    ET.SubElement(programme_el, "episode-num", system="xmltv_ns").text = f"{sea_num-1}.{ep_num-1}."
                ET.SubElement(programme_el, "episode-num", system="onscreen").text = f"S{sea_num:02d}E{ep_num:02d}"
                break

        if airing.get("repeat") is True:
            ET.SubElement(programme_el, "previously-shown")
        if "premiere" in str(airing.get("isPremiereOrFinale", "")).lower():
            ET.SubElement(programme_el, "premiere")

        video_props = airing.get("videoProperties", [])
        if "hdtv" in video_props or "uhdtv" in video_props:
            quality_el = ET.SubElement(programme_el, "video")
            ET.SubElement(quality_el, "quality").text = "HDTV" if "hdtv" in video_props else "UHDTV"

        programme_count += 1

    logger.info(
        "Built %d <programme> entries (%d skipped: missing/invalid data, %d duplicates dropped)",
        programme_count, skipped_count, duplicate_count,
    )
    return tv_el


def main() -> int:
    try:
        config = _load_config()
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    logger.info("Starting EPG fetch (idempotent run)")
    logger.info("Output: %s, Days ahead: %d", config.output_path, config.days_ahead)

    client = SDClient(username=config.username, password=config.password, user_agent=config.user_agent)

    try:
        client.authenticate()

        logger.info("Fetching lineup %s", config.lineup_id)
        lineup_payload = client.get_lineup(config.lineup_id, verbose=True)
        stations = client.stations_from_lineup(lineup_payload)
        if not stations:
            raise SystemExit(f"Lineup {config.lineup_id} returned zero stations")
        logger.info("Lineup has %d stations", len(stations))

        station_ids = [s.station_id for s in stations]
        dates = _daterange(config.days_ahead)
        logger.info("Fetching schedules for %d stations x %d days", len(station_ids), len(dates))
        schedules = client.get_schedules(station_ids, dates)

        program_ids = sorted({airing["programID"] for entry in schedules for airing in entry.get("programs", []) if "programID" in airing})
        logger.info("Fetching metadata for %d distinct programs", len(program_ids))
        programs = client.get_programs(program_ids)

    except SDError as exc:
        logger.error("Schedules Direct request failed: %s", exc)
        return 1

    tv_element = build_xmltv(stations, schedules, programs)

    output_path = config.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ET.indent(tv_element, space="  ")
    tree = ET.ElementTree(tv_element)

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    try:
        # Write to temporary file first -- never touch the live guide.xml
        # directly, so a reader (Plex, Jellyfin, TVHeadend) polling the file
        # mid-write can never observe a half-written document.
        tree.write(tmp_path, encoding="UTF-8", xml_declaration=True)

        # Add DOCTYPE declaration
        with open(tmp_path, "r", encoding="utf-8") as fh:
            contents = fh.read()

        declaration, _, rest = contents.partition("\n")
        contents = f'{declaration}\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n{rest}'

        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(contents)

        # Validate before making it live: well-formedness first (catches
        # encoding/serialization corruption), then XMLTV-specific structural
        # invariants (catches logically-broken-but-well-formed documents,
        # e.g. a programme referencing a channel id that doesn't exist).
        logger.info("Validating XML structure...")
        if not _validate_xml_file(tmp_path):
            logger.error("Generated XML failed well-formedness validation; aborting write")
            tmp_path.unlink()
            return 1
        if not _validate_xmltv_structure(tmp_path):
            logger.error("Generated XML failed structural validation; aborting write")
            tmp_path.unlink()
            return 1

        # Atomic move: this either succeeds entirely or fails, never partial.
        # os.replace() (which Path.replace() wraps) is guaranteed atomic on
        # POSIX and on Windows as of Python 3.3+, so a reader can never see
        # a file that's half old-guide, half new-guide.
        os.replace(tmp_path, output_path)

        final_size = output_path.stat().st_size
        logger.info("Wrote %s (%d bytes)", output_path, final_size)
        logger.info("XML is well-formed and structurally valid")
        return 0

    except Exception as e:
        logger.error("Failed to write output file: %s", e)
        if tmp_path.exists():
            tmp_path.unlink()
        return 1


if __name__ == "__main__":
    sys.exit(main())
