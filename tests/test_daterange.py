"""Unit tests for fetch_epg._daterange, the date-list builder for the
Schedules Direct POST /schedules request body."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetch_epg import _daterange  # noqa: E402


def test_daterange_returns_requested_count() -> None:
    assert len(_daterange(10)) == 10
    assert len(_daterange(1)) == 1


def test_daterange_is_consecutive_utc_dates_starting_today() -> None:
    today = datetime.now(timezone.utc).date()
    dates = _daterange(3)
    expected = [(today + timedelta(days=i)).isoformat() for i in range(3)]
    assert dates == expected


def test_daterange_format_is_iso_yyyy_mm_dd() -> None:
    dates = _daterange(2)
    for d in dates:
        datetime.strptime(d, "%Y-%m-%d")  # raises ValueError if malformed


def test_daterange_rejects_non_positive_input() -> None:
    with pytest.raises(ValueError):
        _daterange(0)
    with pytest.raises(ValueError):
        _daterange(-5)
