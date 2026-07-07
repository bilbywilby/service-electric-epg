"""
Offline smoke test for the XMLTV builder. Runs with zero network access and
zero Schedules Direct credentials -- it only exercises fetch_epg.build_xmltv
against synthetic data shaped like real SD API responses, so it's safe to run
on every push, not just the daily schedule.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetch_epg import build_xmltv  # noqa: E402
from sdclient import Station  # noqa: E402


def _sample_station() -> Station:
    return Station(
        station_id="20454",
        name="WFMZ Allentown",
        callsign="WFMZDT",
        channel="008",
        icon_url="https://example.invalid/logo.png",
    )


def _sample_schedule(station_id: str) -> dict:
    return {
        "stationID": station_id,
        "programs": [
            {
                "programID": "EP012801050074",
                "airDateTime": "2026-07-08T00:00:00Z",
                "duration": 1800,
                "md5": "Sy8HEMBPcuiAx3FBukUhKQ",
                "new": True,
                "videoProperties": ["hdtv"],
            }
        ],
    }


def _sample_programs() -> dict[str, dict]:
    return {
        "EP012801050074": {
            "programID": "EP012801050074",
            "titles": [{"title120": "Local Evening News", "titleLanguage": "en"}],
            "descriptions": {
                "description1000": [
                    {"descriptionLanguage": "en", "description": "Local news, weather, and sports."}
                ]
            },
            "originalAirDate": "2026-07-08",
            "genres": ["News"],
            "episodeTitle150": "July 8 Edition",
            "metadata": [{"Gracenote": {"season": 2026, "episode": 189}}],
        }
    }


def test_build_xmltv_produces_valid_well_formed_xml() -> None:
    station = _sample_station()
    tv = build_xmltv([station], [_sample_schedule(station.station_id)], _sample_programs())

    raw = ET.tostring(tv, encoding="unicode")
    reparsed = ET.fromstring(raw)  # raises if malformed
    assert reparsed.tag == "tv"


def test_build_xmltv_channel_and_programme_counts() -> None:
    station = _sample_station()
    tv = build_xmltv([station], [_sample_schedule(station.station_id)], _sample_programs())

    channels = tv.findall("channel")
    programmes = tv.findall("programme")
    assert len(channels) == 1
    assert len(programmes) == 1


def test_build_xmltv_time_formatting_is_utc_with_offset() -> None:
    station = _sample_station()
    tv = build_xmltv([station], [_sample_schedule(station.station_id)], _sample_programs())

    programme = tv.find("programme")
    assert programme is not None
    assert programme.get("start") == "20260708000000 +0000"
    assert programme.get("stop") == "20260708003000 +0000"


def test_build_xmltv_skips_airings_with_no_matching_program_data() -> None:
    station = _sample_station()
    schedule = _sample_schedule(station.station_id)
    tv = build_xmltv([station], [schedule], programs={})  # no program metadata available

    assert len(tv.findall("programme")) == 0
    assert len(tv.findall("channel")) == 1


def test_build_xmltv_maps_episode_num_zero_indexed() -> None:
    station = _sample_station()
    tv = build_xmltv([station], [_sample_schedule(station.station_id)], _sample_programs())

    episode_num = tv.find("programme/episode-num")
    assert episode_num is not None
    assert episode_num.text == "2025.188."
