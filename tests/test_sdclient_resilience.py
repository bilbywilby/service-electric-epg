"""
Tests for sdclient.py's resilience behavior: retry/backoff configuration,
extra_headers plumbing through _request(), and per-chunk failure isolation
in get_schedules/get_programs. No network access -- the retry adapter's
configuration is inspected directly, and chunk-failure isolation is tested
by monkeypatching _request_with_queue_retry, since exercising the real
urllib3 retry loop would require an actual flaky HTTP server.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sdclient as sdclient_module  # noqa: E402
from sdclient import SDClient, SDError  # noqa: E402


@pytest.fixture
def client() -> SDClient:
    c = SDClient(username="user@example.com", password="hunter2", user_agent="test-agent")
    c._token = "faketoken"  # bypass authenticate() for tests that don't need it
    return c


# ---------------------------------------------------------------------------
# Retry/backoff configuration
# ---------------------------------------------------------------------------

def test_retry_adapter_forces_429_and_5xx(client: SDClient) -> None:
    adapter = client._session.get_adapter("https://json.schedulesdirect.org")
    retry = adapter.max_retries
    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}


def test_retry_adapter_does_not_force_4xx_auth_codes(client: SDClient) -> None:
    """401/403 must never be in status_forcelist: those are permanent
    credential failures the urllib3 adapter should not retry. (In practice
    SD signals auth failure via a JSON body code, not an HTTP status, so
    this is really belt-and-suspenders -- but if that ever changed, this
    guards against silently retrying a hard auth failure four times.)"""
    adapter = client._session.get_adapter("https://json.schedulesdirect.org")
    retry = adapter.max_retries
    assert 401 not in retry.status_forcelist
    assert 403 not in retry.status_forcelist


def test_retry_adapter_has_bounded_total_attempts(client: SDClient) -> None:
    adapter = client._session.get_adapter("https://json.schedulesdirect.org")
    assert adapter.max_retries.total == 4


def test_sd_auth_error_is_not_retried_by_the_queue_retry_loop(client: SDClient) -> None:
    """SDAuthError (and any SDError) raised from _request must propagate
    immediately out of _request_with_queue_retry -- that loop only retries
    the 'still queued' condition, never application errors."""
    call_count = {"n": 0}

    def always_fails(method: str, path: str, **kwargs: object) -> None:
        call_count["n"] += 1
        raise SDError(4001, "invalid credentials")

    with mock.patch.object(client, "_request", side_effect=always_fails):
        with pytest.raises(SDError, match="invalid credentials"):
            client._request_with_queue_retry("GET", "/status")

    assert call_count["n"] == 1  # not retried


# ---------------------------------------------------------------------------
# extra_headers plumbing
# ---------------------------------------------------------------------------

def test_request_passes_extra_headers_to_transport(client: SDClient) -> None:
    captured: dict[str, object] = {}

    def spy(method: str, url: str, **kwargs: object) -> mock.Mock:
        captured.update(kwargs.get("headers", {}))  # type: ignore[arg-type]
        resp = mock.Mock()
        resp.json.return_value = {"code": 0}
        return resp

    with mock.patch.object(client._session, "request", side_effect=spy):
        client._request("GET", "/lineups/USA-NY12345-X", extra_headers={"verboseMap": "true"})

    assert captured.get("verboseMap") == "true"


def test_request_extra_headers_do_not_override_auth_token(client: SDClient) -> None:
    captured: dict[str, object] = {}

    def spy(method: str, url: str, **kwargs: object) -> mock.Mock:
        captured.update(kwargs.get("headers", {}))  # type: ignore[arg-type]
        resp = mock.Mock()
        resp.json.return_value = {"code": 0}
        return resp

    with mock.patch.object(client._session, "request", side_effect=spy):
        client._request("GET", "/lineups/USA-NY12345-X", extra_headers={"verboseMap": "true"})

    assert captured.get("token") == "faketoken"


def test_get_lineup_sets_verbose_map_header_when_requested(client: SDClient) -> None:
    with mock.patch.object(client, "_request", return_value={"map": [], "stations": []}) as m:
        client.get_lineup("USA-NY12345-X", verbose=True)
    _, kwargs = m.call_args
    assert kwargs["extra_headers"] == {"verboseMap": "true"}


def test_get_lineup_omits_verbose_map_header_when_not_requested(client: SDClient) -> None:
    with mock.patch.object(client, "_request", return_value={"map": [], "stations": []}) as m:
        client.get_lineup("USA-NY12345-X", verbose=False)
    _, kwargs = m.call_args
    assert kwargs["extra_headers"] is None


# ---------------------------------------------------------------------------
# Per-chunk failure isolation
# ---------------------------------------------------------------------------

def test_get_schedules_skips_a_fully_failed_chunk_and_keeps_others(
    client: SDClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdclient_module, "MAX_STATIONIDS_PER_SCHEDULE_REQUEST", 1)
    call_log: list[str] = []

    def flaky(method: str, path: str, *, json_body: object = None, params: object = None) -> object:
        station_id = json_body[0]["stationID"]  # type: ignore[index]
        call_log.append(station_id)
        if station_id == "S1":
            raise SDError(5000, "simulated transport failure")
        return [{"stationID": station_id, "programs": [{"programID": "P1", "airDateTime": "2026-07-08T00:00:00Z", "duration": 1800}]}]

    with mock.patch.object(client, "_request_with_queue_retry", side_effect=flaky):
        result = client.get_schedules(["S1", "S2"], ["2026-07-08"])

    assert call_log == ["S1", "S2"]  # both chunks attempted despite the first failing
    assert len(result) == 1
    assert result[0]["stationID"] == "S2"


def test_get_schedules_returns_empty_list_when_every_chunk_fails(
    client: SDClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdclient_module, "MAX_STATIONIDS_PER_SCHEDULE_REQUEST", 1)

    def always_fails(method: str, path: str, *, json_body: object = None, params: object = None) -> object:
        raise SDError(5000, "simulated transport failure")

    with mock.patch.object(client, "_request_with_queue_retry", side_effect=always_fails):
        result = client.get_schedules(["S1", "S2"], ["2026-07-08"])

    assert result == []  # degrades gracefully rather than raising


def test_get_programs_skips_a_fully_failed_chunk_and_keeps_others(
    client: SDClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdclient_module, "MAX_PROGRAMIDS_PER_REQUEST", 1)

    def flaky(method: str, path: str, *, json_body: object = None, params: object = None) -> object:
        program_id = json_body[0]  # type: ignore[index]
        if program_id == "P1":
            raise SDError(5000, "simulated transport failure")
        return [{"programID": program_id, "titles": [{"title120": "Show"}]}]

    with mock.patch.object(client, "_request_with_queue_retry", side_effect=flaky):
        result = client.get_programs(["P1", "P2"])

    assert result == {"P2": {"programID": "P2", "titles": [{"title120": "Show"}]}}


def test_get_schedules_still_skips_individual_stations_within_a_successful_chunk(
    client: SDClient,
) -> None:
    """Pre-existing behavior, not new in this pass: a chunk that succeeds at
    the transport level can still contain a per-station SD error code
    (e.g. an unavailable station) mixed in with good entries; only that one
    station's entry is dropped."""
    payload = [
        {"stationID": "S1", "code": 7000, "message": "station unavailable"},
        {"stationID": "S2", "programs": [{"programID": "P1", "airDateTime": "2026-07-08T00:00:00Z", "duration": 1800}]},
    ]
    with mock.patch.object(client, "_request_with_queue_retry", return_value=payload):
        result = client.get_schedules(["S1", "S2"], ["2026-07-08"])

    assert len(result) == 1
    assert result[0]["stationID"] == "S2"
