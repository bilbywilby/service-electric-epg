#!/usr/bin/env python3
"""
EPG Generator: Fetches schedule data from Schedules Direct and builds an XMLTV file.
"""
import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

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

def _env(key: str, default: Optional[str] = None, required: bool = False) -> str:
    """Get environment variable, raise error if required and missing."""
    value = os.getenv(key, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value

def _daterange(days: int) -> List[str]:
    """Generate a list of ISO date strings for the next N days."""
    today = datetime.now().date()
    return [(today + timedelta(days=i)).isoformat() for i in range(days)]

def build_xmltv(stations: List[Station], schedules: List[Dict], programs: Dict[str, Any]) -> ET.Element:
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
        duration = airing.get("duration", 0)

        if not program_id or not start_time or not station_id:
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

        programme_el = ET.SubElement(tv_el, "programme", start=start_str, stop=stop_str, channel=station_id)

        titles = program_data.get("titles", [])
        title = titles[0].get("title120", "Unknown Title") if titles else "Unknown Title"
        ET.SubElement(programme_el, "title", lang="en").text = title

        if program_data.get("episodeTitle150"):
            ET.SubElement(programme_el, "sub-title", lang="en").text = program_data["episodeTitle150"]

        descriptions = program_data.get("descriptions", {})
        desc_list = descriptions.get("description1000") or descriptions.get("description100") or []
        if desc_list:
            ET.SubElement(programme_el, "desc", lang="en").text = desc_list[0].get("description", "")

        for genre in program_data.get("genres", []):
            ET.SubElement(programme_el, "category", lang="en").text = genre

        if program_data.get("showType"):
            st = program_data["showType"].lower()
            if "movie" in st:
                programme_el.set("category", "movie")
            elif "series" in st:
                programme_el.set("category", "series")

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
            if sea_num and ep_num:
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

    logger.info(f"Built {programme_count} <programme> entries ({skipped_count} skipped: program data missing or invalid)")
    return tv_el

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
            raise SystemExit(f"Lineup {lineup_id} returned zero stations")
        logger.info("Lineup has %d stations", len(stations))

        station_ids = [s.station_id for s in stations]
        dates = _daterange(days_ahead)
        logger.info("Fetching schedules for %d stations x %d days", len(station_ids), len(dates))
        schedules = client.get_schedules(station_ids, dates)

        program_ids = sorted({airing["programID"] for entry in schedules for airing in entry.get("programs", []) if "programID" in airing})
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

    try:
        tree.write(tmp_path, encoding="UTF-8", xml_declaration=True)

        with open(tmp_path, "r", encoding="utf-8") as fh:
            contents = fh.read()

        declaration, _, rest = contents.partition("\n")
        contents = f'{declaration}\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n{rest}'

        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(contents)

        tmp_path.replace(output_path)

        logger.info("Wrote %s (%d bytes)", output_path, output_path.stat().st_size)
        return 0

    except Exception as e:
        logger.error("Failed to write output file: %s", e)
        if tmp_path.exists():
            tmp_path.unlink()
        return 1

if __name__ == "__main__":
    sys.exit(main())
