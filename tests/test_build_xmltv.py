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


def test_build_xmltv_keeps_episode_num_for_season_zero() -> None:
    """Regression test: `if sea_num and ep_num` used to drop this entirely,
    since 0 is falsy in Python but a legitimate season/episode number for
    specials in some catalogs. onscreen (S00E00) is well-defined for
    season/episode 0; xmltv_ns is not (it's zero-indexed relative to
    1-based broadcast numbering, so season 0 has no valid xmltv_ns form)
    and must be omitted rather than emitted as a nonsensical "-1.-1."."""
    station = _sample_station()
    schedule = _sample_schedule(station.station_id)
    programs = _sample_programs()
    programs["EP012801050074"]["metadata"] = [{"Gracenote": {"season": 0, "episode": 0}}]

    tv = build_xmltv([station], [schedule], programs)

    episode_nums = tv.findall("programme/episode-num")
    systems = {e.get("system"): e.text for e in episode_nums}
    assert systems == {"onscreen": "S00E00"}


def test_build_xmltv_show_type_is_a_category_element_not_an_attribute() -> None:
    """Regression test: programme_el.set("category", ...) produced an
    attribute the XMLTV DTD doesn't define on <programme>. Show-type
    category must be a <category> child element instead."""
    station = _sample_station()
    schedule = _sample_schedule(station.station_id)
    programs = _sample_programs()
    programs["EP012801050074"]["showType"] = "Movie"

    tv = build_xmltv([station], [schedule], programs)

    programme = tv.find("programme")
    assert programme is not None
    assert programme.get("category") is None
    categories = [c.text for c in programme.findall("category")]
    assert "Movie" in categories


def _airing_with(**overrides: object) -> dict:
    base = {
        "programID": "EP012801050074",
        "airDateTime": "2026-07-08T00:00:00Z",
        "duration": 1800,
    }
    base.update(overrides)
    return base


def test_build_xmltv_rejects_non_numeric_duration() -> None:
    """A string duration would previously raise an uncaught TypeError from
    timedelta(seconds=duration), crashing the whole build instead of
    skipping one bad airing."""
    station = _sample_station()
    schedule = {"stationID": station.station_id, "programs": [_airing_with(duration="thirty minutes")]}
    tv = build_xmltv([station], [schedule], _sample_programs())
    assert len(tv.findall("programme")) == 0


def test_build_xmltv_rejects_negative_and_zero_duration() -> None:
    station = _sample_station()
    schedule = {
        "stationID": station.station_id,
        "programs": [_airing_with(duration=-600), _airing_with(duration=0)],
    }
    tv = build_xmltv([station], [schedule], _sample_programs())
    assert len(tv.findall("programme")) == 0


def test_build_xmltv_rejects_bool_duration() -> None:
    """bool is a subclass of int in Python; `isinstance(True, (int, float))`
    is True, so a stray boolean has to be excluded explicitly or it would
    silently pass the numeric-type check."""
    station = _sample_station()
    schedule = {"stationID": station.station_id, "programs": [_airing_with(duration=True)]}
    tv = build_xmltv([station], [schedule], _sample_programs())
    assert len(tv.findall("programme")) == 0


def test_build_xmltv_accepts_valid_positive_duration() -> None:
    station = _sample_station()
    schedule = {"stationID": station.station_id, "programs": [_airing_with(duration=1800)]}
    tv = build_xmltv([station], [schedule], _sample_programs())
    assert len(tv.findall("programme")) == 1


def test_build_xmltv_drops_exact_duplicate_airings() -> None:
    """Same (channel, start, title) is treated as the same broadcast --
    keep the first occurrence, drop the rest."""
    station = _sample_station()
    schedule = {
        "stationID": station.station_id,
        "programs": [_airing_with(), _airing_with(), _airing_with()],
    }
    tv = build_xmltv([station], [schedule], _sample_programs())
    assert len(tv.findall("programme")) == 1


def test_build_xmltv_keeps_same_title_different_start_time() -> None:
    """Duplicate detection is keyed on (channel, start, title) together --
    a rerun of the same show at a different time is a distinct airing, not
    a duplicate."""
    station = _sample_station()
    schedule = {
        "stationID": station.station_id,
        "programs": [
            _airing_with(airDateTime="2026-07-08T00:00:00Z"),
            _airing_with(airDateTime="2026-07-08T04:00:00Z"),
        ],
    }
    tv = build_xmltv([station], [schedule], _sample_programs())
    assert len(tv.findall("programme")) == 2


def test_build_xmltv_keeps_same_title_different_channel() -> None:
    """Same show, same start time, but two different stations carrying it
    (a simulcast) are two distinct <programme> entries, not duplicates --
    the channel is part of the dedup key precisely so this isn't collapsed."""
    station_a = _sample_station()
    station_b = Station(station_id="99999", name="Simulcast Station", callsign="WXYZ", channel="099", icon_url=None)
    programs = _sample_programs()
    schedule_a = {"stationID": station_a.station_id, "programs": [_airing_with()]}
    schedule_b = {"stationID": station_b.station_id, "programs": [_airing_with()]}
    tv = build_xmltv([station_a, station_b], [schedule_a, schedule_b], programs)
    assert len(tv.findall("programme")) == 2
