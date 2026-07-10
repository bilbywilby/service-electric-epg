"""
Schedules Direct JSON API (20141201) client.

Implements the subset of the API needed for a lineup-based EPG pull:
token auth, headend/lineup discovery, lineup station maps, schedules,
and program metadata. Built directly against the documented endpoints
at https://github.com/SchedulesDirect/JSON-Service/wiki/API-20141201
(current as of the 2025-12-01 revision).

Design notes:
- Token auth uses a lowercase sha1_hex of the password, sent once;
  the token is cached and reused for its ~24h lifetime.
- SD embeds application-level errors in JSON bodies (sometimes with a
  2xx/4xx HTTP status), so every response is inspected for a "code"
  field in addition to normal HTTP error handling.
- Two classes of transient failure are retried automatically:
  network-level (connection errors, 5xx) via a urllib3 Retry adapter,
  and SD-level "queued, try again" responses (6001 / 7100) via a
  bounded sleep-and-retry loop. Everything else raises immediately.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("sdclient")

DEFAULT_BASE_URL = "https://json.schedulesdirect.org/20141201"
MAX_STATIONIDS_PER_SCHEDULE_REQUEST = 500
MAX_PROGRAMIDS_PER_REQUEST = 2000
QUEUED_RETRY_ATTEMPTS = 4
QUEUED_RETRY_SLEEP_SECONDS = 20.0


class SDError(RuntimeError):
    """Base class for all Schedules Direct application-level errors."""

    def __init__(self, code: int, message: str, raw: dict[str, Any] | None = None) -> None:
        super().__init__(f"SD error {code}: {message}")
        self.code = code
        self.message = message
        self.raw = raw or {}


class SDAuthError(SDError):
    """Bad credentials, expired account, or locked out."""


class SDServiceOfflineError(SDError):
    """SD has signaled a maintenance window; caller should back off ~30-60 min."""


class SDRateLimitedError(SDError):
    """Too many logins / unique IPs / lineup changes in the trailing 24h."""


class SDQuotaExceededError(SDError):
    """Permanent per-request failure, e.g. invalid programID or schedule date out of range."""


# Codes that mean "stop now, don't retry, surface to the operator"
_HARD_FAILURE_CODES: dict[int, type[SDError]] = {
    3000: SDServiceOfflineError,
    4001: SDAuthError,
    4002: SDAuthError,
    4003: SDAuthError,
    4004: SDRateLimitedError,
    4005: SDAuthError,
    4007: SDAuthError,
    4008: SDAuthError,
    4009: SDRateLimitedError,
    4010: SDRateLimitedError,
    6000: SDQuotaExceededError,
    7020: SDQuotaExceededError,
}

# Codes that mean "the data isn't generated yet, sleep and retry the same call"
_QUEUED_CODES = {6001, 7100}


@dataclass
class Station:
    station_id: str
    name: str
    callsign: str
    channel: str
    icon_url: str | None


class SDClient:
    def __init__(
        self,
        username: str,
        password: str,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not username or not password:
            raise ValueError("SD username and password must both be provided")
        self._username = username
        self._password_sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._token: str | None = None
        self._token_expires: float = 0.0

        self._session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            # 429 added: urllib3 will already retry 429 automatically if SD
            # sends a Retry-After header (413/429/503 are special-cased for
            # that regardless of status_forcelist), but that's conditional
            # on the header being present. Listing 429 here unconditionally
            # retries it either way, while respect_retry_after_header
            # (default True) still honors the header's specific wait time
            # when SD does send one, instead of always falling back to the
            # backoff_factor curve.
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST", "PUT", "DELETE"),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Content-Type": "application/json;charset=UTF-8",
                "Accept-Encoding": "deflate,gzip",
            }
        )

    def __enter__(self) -> "SDClient":
        self.authenticate()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Core request plumbing
    # ------------------------------------------------------------------

    def _raise_for_sd_error(self, payload: Any) -> None:
        """Inspect a decoded JSON body for an SD application-level error code."""
        if isinstance(payload, dict) and "code" in payload and payload.get("code", 0) != 0:
            code = int(payload["code"])
            message = str(payload.get("message", "unknown error"))
            if code in _QUEUED_CODES:
                return  # handled by the caller's retry loop
            error_cls = _HARD_FAILURE_CODES.get(code, SDError)
            raise error_cls(code, message, raw=payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, str] | None = None,
        require_token: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = dict(extra_headers or {})
        if require_token:
            if self._token is None:
                raise SDAuthError(0, "authenticate() must be called before other requests")
            headers["token"] = self._token

        response = self._session.request(
            method, url, json=json_body, params=params, headers=headers, timeout=self._timeout
        )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise SDError(0, f"non-JSON response from {path}: {response.text[:200]!r}")

        # A bare list response (e.g. GET /headends, POST /programs) has no
        # top-level "code" to check; only inspect dict-shaped payloads here.
        if isinstance(payload, dict):
            self._raise_for_sd_error(payload)
        return payload

    def _request_with_queue_retry(
        self, method: str, path: str, *, json_body: Any = None, params: dict[str, str] | None = None
    ) -> Any:
        """Like _request, but transparently retries SD 'queued, try again' responses."""
        last_payload: Any = None
        for attempt in range(1, QUEUED_RETRY_ATTEMPTS + 1):
            payload = self._request(method, path, json_body=json_body, params=params)
            last_payload = payload
            if self._payload_is_queued(payload):
                logger.info(
                    "SD reports data still generating (attempt %d/%d) for %s; sleeping %.0fs",
                    attempt,
                    QUEUED_RETRY_ATTEMPTS,
                    path,
                    QUEUED_RETRY_SLEEP_SECONDS,
                )
                time.sleep(QUEUED_RETRY_SLEEP_SECONDS)
                continue
            return payload
        logger.warning("Giving up waiting on queued data for %s after %d attempts", path, QUEUED_RETRY_ATTEMPTS)
        return last_payload

    @staticmethod
    def _payload_is_queued(payload: Any) -> bool:
        if isinstance(payload, dict):
            return payload.get("code") in _QUEUED_CODES
        if isinstance(payload, list):
            return any(isinstance(item, dict) and item.get("code") in _QUEUED_CODES for item in payload)
        return False

    # ------------------------------------------------------------------
    # Auth / status
    # ------------------------------------------------------------------

    def authenticate(self, force_new_token: bool = False) -> None:
        now = time.time()
        if self._token and now < self._token_expires - 60 and not force_new_token:
            return
        body: dict[str, Any] = {"username": self._username, "password": self._password_sha1}
        if force_new_token:
            body["newToken"] = True
        payload = self._request("POST", "/token", json_body=body, require_token=False)
        self._token = payload["token"]
        self._token_expires = float(payload["tokenExpires"])
        logger.info("Authenticated to Schedules Direct; token valid until epoch %d", self._token_expires)

    def get_status(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    # ------------------------------------------------------------------
    # Lineup discovery / management
    # ------------------------------------------------------------------

    def get_headends(self, country: str, postal_code: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", "/headends", params={"country": country, "postalcode": postal_code}
        )
        return payload if isinstance(payload, list) else []

    def add_lineup(self, lineup_id: str) -> None:
        try:
            self._request("PUT", f"/lineups/{lineup_id}")
        except SDError as exc:
            # 2100-series "already in lineup" style responses aren't in the
            # hard-failure map above and surface as generic SDError; treat
            # anything mentioning "already" as a harmless no-op.
            if "already" in exc.message.lower():
                logger.info("Lineup %s is already on this account", lineup_id)
                return
            raise

    def list_lineups(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/lineups")
        return payload.get("lineups", [])

    def get_lineup(self, lineup_id: str, verbose: bool = True) -> dict[str, Any]:
        # verboseMap must ride in the request headers per the API docs.
        # Routed through the shared _request() so this gets the same
        # JSON-decode-error handling and error normalization as every
        # other call, instead of a hand-rolled session.get() that skipped
        # both.
        extra_headers = {"verboseMap": "true"} if verbose else None
        return self._request("GET", f"/lineups/{lineup_id}", extra_headers=extra_headers)

    def stations_from_lineup(self, lineup_payload: dict[str, Any]) -> list[Station]:
        stations_by_id = {s["stationID"]: s for s in lineup_payload.get("stations", [])}
        channel_by_station: dict[str, str] = {}
        for entry in lineup_payload.get("map", []):
            station_id = entry.get("stationID")
            channel = entry.get("channel") or entry.get("virtualChannel") or ""
            if station_id and station_id not in channel_by_station:
                channel_by_station[station_id] = str(channel)

        stations: list[Station] = []
        for station_id, raw in stations_by_id.items():
            icon_url = None
            logos = raw.get("stationLogo") or []
            if logos:
                icon_url = logos[0].get("URL")
            elif raw.get("logo"):
                icon_url = raw["logo"].get("URL")
            stations.append(
                Station(
                    station_id=station_id,
                    name=raw.get("name", raw.get("callsign", station_id)),
                    callsign=raw.get("callsign", ""),
                    channel=channel_by_station.get(station_id, ""),
                    icon_url=icon_url,
                )
            )
        return stations

    # ------------------------------------------------------------------
    # Schedules / programs
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk(items: list[str], size: int) -> Iterator[list[str]]:
        for i in range(0, len(items), size):
            yield items[i : i + size]

    def get_schedules(self, station_ids: list[str], dates: list[str]) -> list[dict[str, Any]]:
        """Fetch schedules for every (station, date) combination requested.

        A single chunk of up to MAX_STATIONIDS_PER_SCHEDULE_REQUEST stations
        failing outright (retries exhausted, or an SD hard-failure code) does
        not abort the whole run -- it's logged and the remaining chunks are
        still attempted, so one bad batch of stations degrades the guide
        instead of blanking it entirely.
        """
        all_entries: list[dict[str, Any]] = []
        chunks = list(self._chunk(station_ids, MAX_STATIONIDS_PER_SCHEDULE_REQUEST))
        for chunk_num, station_chunk in enumerate(chunks, start=1):
            body = [{"stationID": sid, "date": dates} for sid in station_chunk]
            try:
                payload = self._request_with_queue_retry("POST", "/schedules", json_body=body)
            except SDError as exc:
                logger.warning(
                    "Schedule chunk %d/%d (%d stations) failed entirely and was skipped: %s",
                    chunk_num,
                    len(chunks),
                    len(station_chunk),
                    exc,
                )
                continue
            if not isinstance(payload, list):
                logger.warning(
                    "Schedule chunk %d/%d returned unexpected shape %r; skipped",
                    chunk_num,
                    len(chunks),
                    type(payload),
                )
                continue
            for entry in payload:
                if isinstance(entry, dict) and entry.get("code") not in (None, 0):
                    logger.warning(
                        "Schedule error for stationID=%s: code=%s message=%s",
                        entry.get("stationID"),
                        entry.get("code"),
                        entry.get("response") or entry.get("message"),
                    )
                    continue
                all_entries.append(entry)
        return all_entries

    def get_programs(self, program_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch program metadata, keyed by programID. Skips IDs SD can't
        produce, and skips an entire failed chunk with a warning rather than
        aborting the whole run -- missing metadata for a subset of programs
        just means those airings get dropped later in build_xmltv, not that
        the guide fails to build at all."""
        unique_ids = sorted(set(program_ids))
        programs: dict[str, dict[str, Any]] = {}
        chunks = list(self._chunk(unique_ids, MAX_PROGRAMIDS_PER_REQUEST))
        for chunk_num, chunk in enumerate(chunks, start=1):
            try:
                payload = self._request_with_queue_retry("POST", "/programs", json_body=chunk)
            except SDError as exc:
                logger.warning(
                    "Program metadata chunk %d/%d (%d programs) failed entirely and was skipped: %s",
                    chunk_num,
                    len(chunks),
                    len(chunk),
                    exc,
                )
                continue
            if not isinstance(payload, list):
                logger.warning(
                    "Program chunk %d/%d returned unexpected shape %r; skipped",
                    chunk_num,
                    len(chunks),
                    type(payload),
                )
                continue
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                program_id = entry.get("programID")
                if entry.get("code") not in (None, 0):
                    logger.warning(
                        "Skipping programID=%s: code=%s message=%s",
                        program_id,
                        entry.get("code"),
                        entry.get("message"),
                    )
                    continue
                if program_id:
                    programs[program_id] = entry
        return programs
