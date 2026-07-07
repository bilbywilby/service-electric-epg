#!/usr/bin/env python3
"""
Fetch the Service Electric lineup schedule from Schedules Direct and write
an XMLTV file. Designed to be run daily by .github/workflows/update-guide.yml
with no interaction; every failure mode either raises (non-zero exit, so the
workflow's commit step never runs against bad data) or logs a warning and
degrades gracefully (a single unavailable program doesn't blank the guide).

Required environment variables:
    SD_USERNAME     Schedules Direct account email
    SD_PASSWORD     Schedules Direct account password (plaintext; hashed locally)
    SD_LINEUP_ID    Lineup ID from scripts/discover_lineup.py, e.g. USA-PA12345-X

Optional:
    DAYS_AHEAD      Days of schedule to fetch, including today (default: 10)
    OUTPUT_PATH     Where to write the XMLTV file (default: data/guide.xml)
    SD_USER_AGENT   User-Agent sent to Schedules Direct (default below)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from sdclient import SDClient, SDError, Station

DEFAULT_USER_AGENT = "service-electric-epg/1.0 (github-actions daily grabber)"
XMLTV_GENERATOR_NAME = "service-electric-epg"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fetch_epg")


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value or ""


def _daterange(days_ahead: int) -> list[str]:
    today = dt.datetime.now(dt.timezone.utc).date()
    return [(today + dt.timedelta(days=offset)).isoformat() for offset in range(days_ahead)]


def _channel_xmltv_id(station: Station) -> str:
    # Stable, collision-resistant, and human-recognizable in a guide list.
    return f"{station.station_id}.schedulesdirect.org"


def _format_xmltv_time(iso_z_time: str, duration_seconds: int | None = None) -> tuple[str, str | None]:
    """Convert SD's '2026-07-07T23:00:00Z' into XMLTV's '20260707230000 +0000',
    plus the stop time if a duration is supplied."""
    start = dt.datetime.strptime(iso_z_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    start_str = start.strftime("%Y%m%d%H%M%S +0000")
    if duration_seconds is None:
        return start_str, None
    stop = start + dt.timedelta(seconds=duration_seconds)
    return start_str, stop.strftime("%Y%m%d%H%M%S +0000")


def _best_description(program: dict[str, Any]) -> str | None:
    descriptions = program.get("descriptions", {})
    for key in ("description1000", "description100"):
        entries = descriptions.get(key) or []
        for entry in entries:
            if entry.get("description"):
                return str(entry["description"])
    return None


def _episode_num_xmltv_ns(program: dict[str, Any]) -> str | None:
    """Build the zero-indexed 'xmltv_ns' episode-num string SD's season/episode
    metadata maps onto: 'season.episode.part' with each field 0-indexed."""
    for meta in program.get("metadata", []):
        gracenote = meta.get("Gracenote")
        if not gracenote:
            continue
        season = gracenote.get("season")
        episode = gracenote.get("episode")
        if season is None and episode is None:
            continue
        season_str = str(season - 1) if isinstance(season, int) else ""
        episode_str = str(episode - 1) if isinstance(episode, int) else ""
        return f"{season_str}.{episode_str}."
    return None


def build_xmltv(stations: list[Station], schedules: list[dict[str, Any]], programs: dict[str, dict[str, Any]]) -> ET.Element:
    tv = ET.Element(
        "tv",
        attrib={
            "generator-info-name": XMLTV_GENERATOR_NAME,
            "generator-info-url": "https://www.schedulesdirect.org/",
        },
    )

    for station in sorted(stations, key=lambda s: (s.channel or "999999", s.name)):
        channel_el = ET.SubElement(tv, "channel", attrib={"id": _channel_xmltv_id(station)})
        display_name = f"{station.channel} {station.callsign}".strip() if station.channel else station.callsign
        ET.SubElement(channel_el, "display-name").text = display_name or station.name
        ET.SubElement(channel_el, "display-name").text = station.name
        if station.icon_url:
            ET.SubElement(channel_el, "icon", attrib={"src": station.icon_url})

    stations_by_id = {s.station_id: s for s in stations}
    programme_count = 0
    missing_program_count = 0

    for schedule_entry in schedules:
        station_id = schedule_entry.get("stationID")
        station = stations_by_id.get(station_id)
        if station is None:
            continue
        for airing in schedule_entry.get("programs", []):
            program_id = airing.get("programID")
            program = programs.get(program_id)
            if program is None:
                missing_program_count += 1
                continue

            start_str, stop_str = _format_xmltv_time(airing["airDateTime"], airing.get("duration"))
            programme_el = ET.SubElement(
                tv,
                "programme",
                attrib={"start": start_str, "stop": stop_str or start_str, "channel": _channel_xmltv_id(station)},
            )

            titles = program.get("titles", [])
            title_text = titles[0]["title120"] if titles else "Unknown"
            ET.SubElement(programme_el, "title", attrib={"lang": "en"}).text = title_text

            episode_title = program.get("episodeTitle150")
            if episode_title:
                ET.SubElement(programme_el, "sub-title", attrib={"lang": "en"}).text = episode_title

            description = _best_description(program)
            if description:
                ET.SubElement(programme_el, "desc", attrib={"lang": "en"}).text = description

            for genre in program.get("genres", []):
                ET.SubElement(programme_el, "category", attrib={"lang": "en"}).text = genre

            original_air_date = program.get("originalAirDate")
            if original_air_date:
                ET.SubElement(programme_el, "date").text = original_air_date.replace("-", "")

            episode_ns = _episode_num_xmltv_ns(program)
            if episode_ns:
                ET.SubElement(
                    programme_el, "episode-num", attrib={"system": "xmltv_ns"}
                ).text = episode_ns

            if airing.get("new") is True:
                ET.SubElement(programme_el, "new")
            if airing.get("repeat") is True:
                ET.SubElement(programme_el, "previously-shown")
            if airing.get("premiere") is True:
                ET.SubElement(programme_el, "premiere")

            video_props = airing.get("videoProperties", [])
            if "hdtv" in video_props or "uhdtv" in video_props:
                quality_el = ET.SubElement(programme_el, "video")
                ET.SubElement(quality_el, "quality").text = "HDTV" if "hdtv" in video_props else "UHDTV"

            programme_count += 1

    logger.info("Built %d <programme> entries (%d skipped: program data unavailable)", programme_count, missing_program_count)
    return tv


def main() -> int:
    username = _env("SD_USERNAME", required=True)
    password = _env("SD_PASSWORD", required=True)
    lineup_id = _env("SD_LINEUP_ID", required=True)
    days_ahead = int(_env("DAYS_AHEAD", "10"))
    output_path = Path(_env("OUTPUT_PATH", "data/guide.xml"))
    user_agent = _env("SD_USER_AGENT", DEFAULT_USER_AGENT)

    client = SDClient(username=username, password=password, user_agent=user_agent)

    try:
        client.authenticate()

        logger.info("Fetching lineup %s", lineup_id)
        lineup_payload = client.get_lineup(lineup_id, verbose=True)
        stations = client.stations_from_lineup(lineup_payload)
        if not stations:
            raise SystemExit(f"Lineup {lineup_id} returned zero stations; refusing to write an empty guide.")
        logger.info("Lineup has %d stations", len(stations))

        station_ids = [s.station_id for s in stations]
        dates = _daterange(days_ahead)
        logger.info("Fetching schedules for %d stations x %d days", len(station_ids), len(dates))
        schedules = client.get_schedules(station_ids, dates)

        program_ids = sorted({airing["programID"] for entry in schedules for airing in entry.get("programs", [])})
        logger.info("Fetching metadata for %d distinct programs", len(program_ids))
        programs = client.get_programs(program_ids)

    except SDError as exc:
        logger.error("Schedules Direct request failed: %s", exc)
        return 1

    tv_element = build_xmltv(stations, schedules, programs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tv_element, space="  ")
    tree = ET.ElementTree(tv_element)

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tree.write(tmp_path, encoding="UTF-8", xml_declaration=True)

    # Insert the XMLTV DOCTYPE, which ElementTree has no native support for.
    with open(tmp_path, "r", encoding="utf-8") as fh:
        contents = fh.read()
    declaration, _, rest = contents.partition("\n")
    contents = f'{declaration}\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n{rest}'
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(contents)
    tmp_path.replace(output_path)

    logger.info("Wrote %s (%d bytes)", output_path, output_path.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
