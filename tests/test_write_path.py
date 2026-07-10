"""
Tests for the write path: XMLTV structural validation, atomic replace, and
startup config validation. All offline -- no network, no SD credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetch_epg import Config, _load_config, _validate_xml_file, _validate_xmltv_structure  # noqa: E402

VALID_XMLTV = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<tv>\n'
    '  <channel id="20454"><display-name>WFMZ</display-name></channel>\n'
    '  <programme channel="20454" start="20260708000000 +0000" stop="20260708003000 +0000">\n'
    "    <title>Local Evening News</title>\n"
    "  </programme>\n"
    "</tv>\n"
)


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

def test_valid_xmltv_passes_both_validators(tmp_path: Path) -> None:
    p = tmp_path / "guide.xml"
    p.write_text(VALID_XMLTV)
    assert _validate_xml_file(p) is True
    assert _validate_xmltv_structure(p) is True


def test_malformed_xml_fails_well_formedness_check(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text("<tv><channel id='20454'></tv>")  # unclosed <channel>
    assert _validate_xml_file(p) is False


def test_wrong_root_element_fails_structural_check(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text('<?xml version="1.0"?><rss></rss>')
    assert _validate_xml_file(p) is True  # well-formed
    assert _validate_xmltv_structure(p) is False  # but not a <tv> root


def test_programme_without_channel_attribute_fails(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text('<?xml version="1.0"?><tv><channel id="A"/><programme start="x"><title>T</title></programme></tv>')
    assert _validate_xmltv_structure(p) is False


def test_programme_referencing_unknown_channel_fails(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text('<?xml version="1.0"?><tv><channel id="A"/><programme channel="B" start="x"><title>T</title></programme></tv>')
    assert _validate_xmltv_structure(p) is False


def test_duplicate_channel_ids_fail(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text('<?xml version="1.0"?><tv><channel id="A"/><channel id="A"/></tv>')
    assert _validate_xmltv_structure(p) is False


def test_programme_without_title_fails(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text('<?xml version="1.0"?><tv><channel id="A"/><programme channel="A" start="x"/></tv>')
    assert _validate_xmltv_structure(p) is False


def test_channel_without_id_fails(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text('<?xml version="1.0"?><tv><channel/></tv>')
    assert _validate_xmltv_structure(p) is False


def test_empty_tv_document_is_structurally_valid(tmp_path: Path) -> None:
    """No channels and no programmes isn't itself a structural violation --
    it's a legitimately empty guide (e.g. a lineup with zero stations would
    already be caught earlier in main(), but the validator itself shouldn't
    conflate 'empty' with 'broken')."""
    p = tmp_path / "empty.xml"
    p.write_text('<?xml version="1.0"?><tv></tv>')
    assert _validate_xmltv_structure(p) is True


# ---------------------------------------------------------------------------
# Atomic write behavior
# ---------------------------------------------------------------------------

def test_os_replace_is_atomic_and_leaves_no_intermediate_state(tmp_path: Path) -> None:
    """Simulates the write path's final step directly: write to a .tmp file,
    then os.replace() into the live path. Confirms the live path either has
    the fully-old or fully-new content -- there's no window where a reader
    could see a truncated or partial file, because os.replace() is a single
    atomic filesystem rename on POSIX."""
    live = tmp_path / "guide.xml"
    live.write_text("OLD CONTENT")

    tmp = tmp_path / "guide.xml.tmp"
    tmp.write_text(VALID_XMLTV)

    os.replace(tmp, live)

    assert live.read_text() == VALID_XMLTV
    assert not tmp.exists()  # the .tmp file no longer exists post-replace
    assert live.exists()


def test_os_replace_works_even_when_no_prior_file_exists(tmp_path: Path) -> None:
    """First-ever run: no live guide.xml yet. os.replace() must still work
    (this is the equivalent of `mv`, not requiring the destination to
    pre-exist)."""
    live = tmp_path / "guide.xml"
    tmp = tmp_path / "guide.xml.tmp"
    tmp.write_text(VALID_XMLTV)

    assert not live.exists()
    os.replace(tmp, live)
    assert live.read_text() == VALID_XMLTV


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SD_USERNAME", "SD_PASSWORD", "SD_LINEUP_ID", "DAYS_AHEAD", "OUTPUT_PATH", "SD_USER_AGENT"):
        monkeypatch.delenv(key, raising=False)


def _set_valid_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str) -> None:
    env = {
        "SD_USERNAME": "user@example.com",
        "SD_PASSWORD": "hunter2",
        "SD_LINEUP_ID": "USA-NY12345-X",
        "DAYS_AHEAD": "10",
        "OUTPUT_PATH": str(tmp_path / "guide.xml"),
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_valid_config_loads(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_valid_env(monkeypatch, tmp_path)
    cfg = _load_config()
    assert isinstance(cfg, Config)
    assert cfg.username == "user@example.com"
    assert cfg.days_ahead == 10
    assert cfg.lineup_id == "USA-NY12345-X"


def test_missing_username_fails_fast(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_valid_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SD_USERNAME")
    with pytest.raises(ValueError, match="SD_USERNAME"):
        _load_config()


def test_username_without_at_sign_fails(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_valid_env(monkeypatch, tmp_path, SD_USERNAME="not-an-email")
    with pytest.raises(ValueError, match="SD_USERNAME"):
        _load_config()


def test_malformed_lineup_id_fails(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_valid_env(monkeypatch, tmp_path, SD_LINEUP_ID="not-a-lineup-id")
    with pytest.raises(ValueError, match="SD_LINEUP_ID"):
        _load_config()


def test_non_integer_days_ahead_fails(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_valid_env(monkeypatch, tmp_path, DAYS_AHEAD="not-a-number")
    with pytest.raises(ValueError, match="DAYS_AHEAD"):
        _load_config()


def test_zero_days_ahead_fails(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_valid_env(monkeypatch, tmp_path, DAYS_AHEAD="0")
    with pytest.raises(ValueError, match="DAYS_AHEAD"):
        _load_config()


def test_unwritable_output_directory_fails(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root: permission checks don't apply")
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)
    try:
        _set_valid_env(monkeypatch, tmp_path, OUTPUT_PATH=str(readonly_dir / "guide.xml"))
        with pytest.raises(ValueError, match="OUTPUT_PATH"):
            _load_config()
    finally:
        readonly_dir.chmod(0o755)


def test_multiple_config_problems_are_all_reported_together(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All problems should surface in one error, not just the first one
    found -- so a person fixes everything in one pass instead of hitting a
    new failure on every retry."""
    _set_valid_env(monkeypatch, tmp_path, SD_USERNAME="bad", SD_LINEUP_ID="bad", DAYS_AHEAD="bad")
    with pytest.raises(ValueError) as exc_info:
        _load_config()
    message = str(exc_info.value)
    assert "SD_USERNAME" in message
    assert "SD_LINEUP_ID" in message
    assert "DAYS_AHEAD" in message
