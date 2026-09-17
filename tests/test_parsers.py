"""Tests for parsing BluOS XML, against responses from real players."""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.bluesound_alt.coordinator import (
    _parse_browse_items,
    _parse_status,
    _safe_int,
)

from .conftest import fixture_text
from .const import LABEL, MASTER, SLAVE_1, SOUNDBAR

# -- /Status ----------------------------------------------------------------


def test_status_master_on_digital_input() -> None:
    """A group master streaming its S/PDIF input."""
    data = _parse_status(fixture_text(LABEL, MASTER, "status"))

    assert data.state == "stream"
    assert data.volume == 34
    assert data.muted is False
    assert data.shuffle is False
    assert data.repeat == 0
    assert data.title == "PlexAmp"
    assert data.artist is None
    assert data.album is None
    assert data.image_url == "/images/capture/ic_streamingNP.png"
    assert data.position == 5887.0
    assert data.duration is None
    assert data.service == "Capture"
    assert data.service_name is None
    assert data.stream_url == "Capture:hw:imxspdif,0/1/25/2?id=input2"
    assert data.group_name == "Player 1 + 2"
    assert data.sync_stat == "37"
    assert data.etag == "f2dcb25b54c99beec284a0151d02e6e6"


def test_status_slave_mirrors_master_playback() -> None:
    """A slave reports the group's playback, not a source of its own."""
    master = _parse_status(fixture_text(LABEL, MASTER, "status"))
    slave = _parse_status(fixture_text(LABEL, SLAVE_1, "status"))

    assert slave.stream_url == master.stream_url
    assert slave.title == master.title
    assert slave.group_name == master.group_name
    # The group volume, not the slave's own; that comes from /Volume.
    assert slave.volume == master.volume


def test_status_standalone_soundbar() -> None:
    """A standalone soundbar on HDMI eARC has no group."""
    data = _parse_status(fixture_text(LABEL, SOUNDBAR, "status"))

    assert data.state == "stream"
    assert data.title == "HDMI eARC"
    assert data.volume == 61
    assert data.stream_url == "Capture:hdmi-input?id=input4"
    assert data.group_name is None


def test_status_long_poll_has_same_shape() -> None:
    """The long-poll answer is an ordinary /Status document."""
    status = _parse_status(fixture_text(LABEL, MASTER, "status"))
    long_poll = _parse_status(fixture_text(LABEL, MASTER, "status_long_poll"))

    # Captured a few seconds apart, so playback has moved on.
    assert dataclasses.replace(long_poll, position=status.position) == status


def test_status_with_track_metadata() -> None:
    """Track playback fills in artist, album and duration."""
    data = _parse_status(
        "<status etag='e'><state>play</state><title1>So What</title1>"
        "<title2>Miles Davis</title2><title3>Kind of Blue</title3>"
        "<secs>12</secs><totlen>545</totlen><mute>1</mute><shuffle>1</shuffle>"
        "<repeat>1</repeat><volume>20</volume><service>Tidal</service>"
        "<serviceName>TIDAL</serviceName></status>"
    )

    assert data.state == "play"
    assert (data.title, data.artist, data.album) == (
        "So What",
        "Miles Davis",
        "Kind of Blue",
    )
    assert (data.position, data.duration) == (12.0, 545.0)
    assert data.muted is True
    assert data.shuffle is True
    assert data.repeat == 1
    assert data.service_name == "TIDAL"


def test_status_tolerates_garbage_numbers() -> None:
    """Unparseable numbers fall back rather than raising."""
    data = _parse_status(
        "<status><volume>loud</volume><repeat/><secs>x</secs>"
        "<totlen>?</totlen></status>"
    )

    assert data.volume == 0
    assert data.repeat == 0
    assert data.position is None
    assert data.duration is None


def test_status_empty_document() -> None:
    """A status with nothing in it gives the defaults."""
    data = _parse_status("<status/>")

    assert data.state == "stop"
    assert data.title is None


# -- /Browse ----------------------------------------------------------------


def test_browse_root_lists_inputs_and_services() -> None:
    """The top level mixes playable inputs with navigable services."""
    items = _parse_browse_items(fixture_text(LABEL, MASTER, "browse_root"))

    assert [i["name"] for i in items] == [
        "Playlists",
        "PlexAmp",
        "Vinyl",
        "Radio",
        "Radio Paradise",
        "Spotify",
        "TuneIn",
    ]
    by_name = {i["name"]: i for i in items}
    assert by_name["TuneIn"] == {
        "name": "TuneIn",
        "image": "/Sources/images/TuneInIcon.png",
        "browse_key": "TuneIn:",
        "play_url": None,
    }
    # Kept exactly as the player encoded it, so it can be replayed verbatim.
    assert (
        by_name["PlexAmp"]["play_url"]
        == "/Play?url=Capture%3Ahw%3Aimxspdif%2C0%2F1%2F25%2F2%3Fid%3Dinput2"
    )
    assert by_name["PlexAmp"]["browse_key"] is None


def test_browse_keys_are_unescaped_once() -> None:
    """XML escaping is undone, percent-encoding is left for the player."""
    items = _parse_browse_items(fixture_text(LABEL, MASTER, "browse_d1_02"))

    norway = next(i for i in items if i["name"] == "Norway")
    assert norway["browse_key"] == (
        "Airable:BrowseMenu/%2FRadioBrowse%3Fservice=Airable"
        "&url=https%253A%252F%252F0000000000.airable.io%252Fradio%252Fplace"
        "%252F9852856029290707"
    )


def test_browse_tunein_menu() -> None:
    """TuneIn's top menu is all links."""
    items = _parse_browse_items(fixture_text(LABEL, MASTER, "browse_d1_04"))

    assert len(items) == 10
    assert items[0]["name"] == "For You"
    assert all(i["browse_key"] and i["play_url"] is None for i in items)


def test_browse_station_list_is_playable() -> None:
    """Station lists carry play URLs and nothing to navigate into."""
    items = _parse_browse_items(fixture_text(LABEL, MASTER, "browse_d2_05"))

    assert items[0]["name"] == "NRK P1+"
    assert all(i["play_url"] and i["browse_key"] is None for i in items)


def test_browse_submenu_of_links() -> None:
    """A submenu of categories is all navigation."""
    items = _parse_browse_items(fixture_text(LABEL, MASTER, "browse_d2_06"))

    assert len(items) == 8
    assert all(i["browse_key"] and i["play_url"] is None for i in items)


def test_browse_empty_menu() -> None:
    """An empty playlist menu gives no items."""
    assert _parse_browse_items(fixture_text(LABEL, MASTER, "browse_d1_01")) == []


def test_browse_legacy_radiotime() -> None:
    """Old radiotime menus use @URL and @key instead of playURL/browseKey."""
    items = _parse_browse_items(
        "<radiotime>"
        '<item text="NRK P1" type="audio" URL="TuneIn:s1234" image="i.png"/>'
        '<item text="Local" type="link" key="radiotime:local"/>'
        '<item type="audio" URL="TuneIn:nameless"/>'
        "</radiotime>"
    )

    assert items == [
        {
            "name": "NRK P1",
            "image": "i.png",
            "browse_key": None,
            "play_url": "/Play?url=TuneIn:s1234",
        },
        {
            "name": "Local",
            "image": None,
            "browse_key": "radiotime:local",
            "play_url": None,
        },
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("browse_d1_03", 14),  # Radio Paradise: MQA and CD Quality
        ("browse_d2_11", 70),  # TuneIn For You: recents, recommendations
        ("browse_d2_12", 4),  # TuneIn Favorites
        ("browse_d2_13", 38),  # TuneIn Local Radio
    ],
)
def test_browse_categorised_menus(name: str, expected: int) -> None:
    """Menus that group their items into categories still list them."""
    items = _parse_browse_items(fixture_text(LABEL, MASTER, name))

    assert len(items) == expected


# -- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [("42", 0, 42), (7, 0, 7), (None, 0, 0), ("x", 11000, 11000), ("", 5, 5)],
)
def test_safe_int(value: object, default: int, expected: int) -> None:
    """Integers parse, everything else falls back to the default."""
    assert _safe_int(value, default) == expected
