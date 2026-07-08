#!/usr/bin/env python3
"""
EPG Generator: Fetches schedule data from Schedules Direct and builds an XMLTV file.
"""
import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

# Try to import requests, fail gracefully if missing
try:
    import requests
except ImportError:
    print("Error: 'requests' library is required. Install with: pip install requests")
    sys.exit(1)

# --- Configuration & Constants ---
DEFAULT_USER_AGENT = "ServiceElectricEPG/1.0 (Automated EPG Generator)"
SD_API_BASE = "https://json.schedulesdirect.org/20141201"

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---
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

# --- Schedules Direct Client ---
class SDError(Exception):
    """Custom exception for Schedules Direct API errors."""
    pass

class SDClient:
    def __init__(self, username: str, password: str, user_agent: str = DEFAULT_USER_AGENT):
        self.username = username
        self.password = password
        self.user_agent = user_agent
        self.token: Optional[str] = None
        self.token_expires: Optional[datetime] = None
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    def authenticate(self) -> None:
        """Authenticate with Schedules Direct and obtain a token."""
        if self.token and self.token_expires and datetime.now() < self.token_expires:
            return  # Token still valid

        logger.info("Authenticating with Schedules Direct...")
        payload = {
            "username": self.username,
            "password": hashlib.sha1(self.password.encode()).hexdigest()
        }
        try:
            resp = self.session.post(f"{SD_API_BASE}/token", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                self.token = data["token"]
                # Token expires in 24 hours, but we'll refresh 1 hour early
                self.token_expires = datetime.now() + timedelta(hours=23)
                self.session.headers.update({"token": self.token})
                logger.info("Authentication successful.")
            else:
                raise SDError(f"Auth failed: {data.get('message', 'Unknown error')}")
        except requests.RequestException as e:
            raise SDError(f"Authentication request failed: {e}")

    def get_lineup(self, lineup_id: str, verbose: bool = False) -> Dict[str, Any]:
        """Fetch lineup details."""
        self.authenticate()
        try:
            resp = self.session.get(f"{SD_API_BASE}/lineups/{lineup_id}")
            resp.raise_for_status()
            data = resp.json()
            errors = [x for x in data if isinstance(x, dict) and x.get("code")]
            if errors:
                raise SDError(f"{len(errors)} station(s) returned errors: {errors[:3]}")
            return data
        except requests.RequestException as e:
            raise SDError(f"Failed to fetch lineup: {e}")

    def stations_from_lineup(self, lineup_payload: Dict[str, Any]) -> List[Dict[str, str]]:
        """Extract station list from lineup payload."""
        stations = []
        for map_entry in lineup_payload.get("map", []):
            station_id = map_entry.get("stationID")
            if station_id:
                stations.append({"station_id": station_id, "channel": map_entry.get("channel", "")})
        return stations

    def get_schedules(self, station_ids: List[str], dates: List[str]) -> List[Dict[str, Any]]:
        """Fetch schedule entries for given stations and dates."""
        self.authenticate()
        if not station_ids or not dates:
            return []

        payload = [{"stationID": station_id, "date": dates} for station_id in station_ids]
        try:
            # Schedules Direct limits requests to 1 hour of data per call usually, 
            # but the /schedules/endpoint allows a range. We'll use the program endpoint later.
            # Actually, the standard flow is:
            # 1. Get schedules (returns programIDs and air times)
            # 2. Get programs (details)
            
            # Note: The API expects a specific structure for schedule requests.
            # We will request day by day to stay safe within limits if needed, 
            # but the endpoint supports a range.
            
            resp = self.session.post(f"{SD_API_BASE}/schedules", json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise SDError(f"Failed to fetch schedules: {e}")

    def get_programs(self, program_ids: Set[str]) -> Dict[str, Any]:
        """Fetch detailed metadata for a set of program IDs."""
        self.authenticate()
        if not program_ids:
            return {}

        # API limits to 5000 IDs per request
        all_programs = {}
        id_list = list(program_ids)
        for i in range(0, len(id_list), 5000):
            batch = id_list[i:i+5000]
            try:
                resp = self.session.post(f"{SD_API_BASE}/programs", json=batch)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for prog in data:
                        all_programs[prog["programID"]] = prog
                else:
                    logger.warning(f"Unexpected response format for programs: {type(data)}")
            except requests.RequestException as e:
                logger.error(f"Failed to fetch program batch: {e}")
                continue
        return all_programs

# --- XMLTV Builder ---
def build_xmltv(stations: List[Dict], schedules: List[Dict], programs: Dict[str, Any]) -> ET.Element:
    """Build the XMLTV ElementTree from fetched data."""
    tv_el = ET.Element("tv")
    tv_el.set("source-info-url", "https://www.schedulesdirect.org/")
    tv_el.set("source-info-name", "Schedules Direct")
    tv_el.set("generator-info-name", "ServiceElectricEPG")
    tv_el.set("generator-info-url", "https://github.com/bilbywilby/service-electric-epg")

    station_map = {s["station_id"]: s for s in stations}
    for station in stations:
        channel_el = ET.SubElement(tv_el, "channel", id=station["station_id"])
        ET.SubElement(channel_el, "display-name").text = f"{station.get('channel', '')} {station['station_id']}".strip()

    programme_count = 0
    skipped_count = 0

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
            start_str = dt_start.strftime("%Y%m%d%H%M%S +0000")
            dt_end = dt_start + timedelta(seconds=duration)
            stop_str = dt_end.strftime("%Y%m%d%H%M%S +0000")
        except ValueError:
            skipped_count += 1
            continue

        programme_el = ET.SubElement(tv_el, "programme", start=start_str, stop=stop_str, channel=station_id)

        titles = program_data.get("titles", [])
        title_text = "Unknown Title"
        if titles and isinstance(titles, list):
            title_text = titles[0].get("title120", titles[0].get("title70", "Unknown Title"))
        ET.SubElement(programme_el, "title", lang="en").text = title_text

        episode_title = program_data.get("episodeTitle150")
        if episode_title:
            ET.SubElement(programme_el, "sub-title", lang="en").text = episode_title

        descriptions = program_data.get("descriptions", {})
        desc_text = None
        for desc_len in ["description100", "description1000", "description10000"]:
            if desc_len in descriptions and isinstance(descriptions[desc_len], list):
                desc_text = descriptions[desc_len][0].get("description")
                break
        if desc_text:
            ET.SubElement(programme_el, "desc", lang="en").text = desc_text

        for genre in program_data.get("genres", []):
            ET.SubElement(programme_el, "category", lang="en").text = genre

        show_type = program_data.get("eventDetails", {}).get("subType", "").lower()
        if not show_type and program_data.get("showType"):
            show_type = program_data.get("showType").lower()
            
        if "movie" in show_type or show_type == "feature film":
            programme_el.set("category", "movie")
        elif "series" in show_type:
            programme_el.set("category", "series")

        if program_data.get("originalAirDate"):
            ET.SubElement(programme_el, "date").text = program_data["originalAirDate"].replace("-", "")

        for rating in program_data.get("contentRating", []):
            if rating.get("code"):
                rating_el = ET.SubElement(programme_el, "rating", system=rating.get("body", "MPAA"))
                ET.SubElement(rating_el, "value").text = rating["code"]

        season_num = None
        episode_num = None
        for meta in program_data.get("metadata", []):
            if "Gracenote" in meta:
                season_num = meta["Gracenote"].get("season")
                episode_num = meta["Gracenote"].get("episode")
                break
        
        if season_num and episode_num:
            try:
                ET.SubElement(programme_el, "episode-num", system="xmltv_ns").text = f"{int(season_num)-1}.{int(episode_num)-1}.0"
                ET.SubElement(programme_el, "episode-num", system="onscreen").text = f"S{int(season_num):02d}E{int(episode_num):02d}"
            except ValueError:
                pass

        if airing.get("new") is True or airing.get("premiere") is True:
            ET.SubElement(programme_el, "premiere")
        elif airing.get("repeat") is True:
            ET.SubElement(programme_el, "previously-shown")

        video_props = airing.get("videoProperties", [])
        if "hdtv" in video_props or "uhdtv" in video_props:
            quality_el = ET.SubElement(programme_el, "video")
            ET.SubElement(quality_el, "quality").text = "HDTV" if "hdtv" in video_props else "UHDTV"

        programme_count += 1

    logger.info(f"Built {programme_count} <programme> entries ({skipped_count} skipped)")
    return tv_el

# --- Main Execution ---
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

        station_ids = [s["station_id"] for s in stations]
        dates = _daterange(days_ahead)
        logger.info("Fetching schedules for %d stations x %d days", len(station_ids), len(dates))
        schedules = client.get_schedules(station_ids, dates)

        program_ids = sorted({airing["programID"] for entry in schedules for airing in entry.get("programs", []) if "programID" in airing})
        logger.info("Fetching metadata for %d distinct programs", len(program_ids))
        programs = client.get_programs(set(program_ids))

    except SDError as exc:
        logger.error("Schedules Direct request failed: %s", exc)
        return 1

    tv_element = build_xmltv(stations, schedules, programs)

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Pretty print the XML
    ET.indent(tv_element, space="  ")
    tree = ET.ElementTree(tv_element)

    # --- ATOMIC WRITE STRATEGY ---
    # 1. Define temp path
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    
    try:
        # 2. Write to temp file
        tree.write(tmp_path, encoding="UTF-8", xml_declaration=True)
        
        # 3. Inject DOCTYPE (ElementTree doesn't support this natively)
        with open(tmp_path, "r", encoding="utf-8") as fh:
            contents = fh.read()
        
        declaration, _, rest = contents.partition("\n")
        # Insert DOCTYPE after XML declaration
        contents = f'{declaration}\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n{rest}'
        
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(contents)
        
        # 4. Atomic rename
        tmp_path.replace(output_path)
        
        logger.info("Wrote %s (%d bytes)", output_path, output_path.stat().st_size)
        return 0
        
    except Exception as e:
        logger.error("Failed to write output file: %s", e)
        # Cleanup temp file if it exists
        if tmp_path.exists():
            tmp_path.unlink()
        return 1

if __name__ == "__main__":
    sys.exit(main())
