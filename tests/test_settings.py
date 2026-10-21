"""Tests for a player's audio settings: listening mode, tone, EQ and limits."""

from __future__ import annotations

import re
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.bluesound_alt.const import DOMAIN
from custom_components.bluesound_alt.coordinator import (
    BluesoundCoordinator,
    _parse_settings,
)

from .conftest import FakeBluOS, fixture_text, setup_player, wait_for
from .const import (
    BLUOS_418,
    BLUOS_418_LABEL,
    BLUOS_418_MAC,
    LABEL,
    MASTER,
    MASTER_MAC,
    SLAVE_1,
    SLAVE_1_MAC,
    SOUNDBAR,
    SOUNDBAR_MAC,
)

MODE = "select.player_4_listening_mode"
SETTINGS = ("/Settings", frozenset({("id", "audio"), ("schemaVersion", "34")}))


def soundbar_menu() -> str:
    """Return the soundbar's captured Audio menu."""
    return fixture_text(LABEL, SOUNDBAR, "settings_audio")


def sent(patch_session: FakeBluOS, path: str) -> list[dict]:
    """Return the bodies posted to a path."""
    return [r.body for r in patch_session.requests if r.path == path]


async def answer_with(patch_session: FakeBluOS, menu: str) -> None:
    """Have the soundbar report a changed Audio menu from now on."""
    patch_session.player(SOUNDBAR).overrides[SETTINGS] = (200, menu)


def test_menu_is_parsed() -> None:
    """Settings take their menu's url when they have none, and resets are left out."""
    settings = _parse_settings(soundbar_menu())

    assert settings["preset"].url == "/alsa_setting"
    assert settings["volumeLimits"].url == "/audiomodes"
    assert settings["volumeLimits"].min_range == 30
    assert settings["eq-bass"].step == 0.5
    assert "reset" not in settings


async def test_soundbar_entities(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """The soundbar shows its menu as the app does."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    mode = hass.states.get(MODE)
    assert mode.state == "Movie"
    assert mode.attributes["options"] == ["Off", "Music", "Movie", "Late Night"]
    assert hass.states.get("select.player_4_replay_gain").state == "Disabled"
    assert hass.states.get("switch.player_4_surround_upmixer").state == "on"
    assert hass.states.get("switch.player_4_deep_bass").state == "off"
    bass = hass.states.get("number.player_4_bass")
    assert (bass.state, bass.attributes["min"], bass.attributes["step"]) == (
        "0.0",
        -6.0,
        0.5,
    )
    assert hass.states.get("number.player_4_minimum_volume").state == "-75.0"
    assert hass.states.get("number.player_4_maximum_volume").state == "-12.0"


async def test_listening_mode_only_where_offered(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The C700's menus have no listening mode, so it gets none."""
    await setup_player(hass, MASTER, MASTER_MAC)

    ent_reg = er.async_get(hass)
    assert not ent_reg.async_get_entity_id(
        "select", DOMAIN, f"{MASTER_MAC}_listening_mode"
    )


async def test_select_listening_mode(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Picking a mode posts its name as the app does; the state follows."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)
    await answer_with(
        patch_session,
        soundbar_menu().replace('list" value="movie"', 'list" value="music"'),
    )

    await hass.services.async_call(
        "select", "select_option", {"entity_id": MODE, "option": "Music"}, blocking=True
    )

    assert sent(patch_session, "/alsa_setting") == [{"preset": "music"}]
    assert hass.states.get(MODE).state == "Music"


async def test_switch_and_tone(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """Switches send ON/OFF, tones send numbers, both as the app does."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.player_4_surround_upmixer"},
        blocking=True,
    )
    for value in (-0.5, 2):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": "number.player_4_treble", "value": value},
            blocking=True,
        )

    assert sent(patch_session, "/alsa_setting") == [
        {"eq-upmix": "OFF"},
        {"eq-treble": -0.5},
        {"eq-treble": 2},
    ]


async def test_volume_limits(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A limit is sent with the other end, and must keep the ends apart."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.player_4_maximum_volume", "value": -13},
        blocking=True,
    )
    assert sent(patch_session, "/audiomodes") == [{"volumeLimits": "-75,-13"}]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": "number.player_4_minimum_volume", "value": -30},
            blocking=True,
        )


async def test_setting_left_out_is_unavailable(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A setting the menu stops listing shows as unavailable."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)
    menu = re.sub(r'<setting id="eq-excursion2".*?</setting>', "", soundbar_menu())
    await answer_with(patch_session, menu)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh_settings()
    await hass.async_block_till_done()

    assert hass.states.get("switch.player_4_deep_bass").state == "unavailable"


async def test_mode_from_the_app_is_followed(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A mode changed in the app moves /SyncStatus, and the entity follows."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)
    player = patch_session.player(SOUNDBAR)
    await answer_with(
        patch_session,
        soundbar_menu().replace('list" value="movie"', 'list" value="night"'),
    )

    player.push(
        "/SyncStatus",
        player.captured("sync_status").replace('etag="841"', 'etag="842"'),
    )

    await wait_for(lambda: hass.states.get(MODE).state == "Late Night")


# -- other players: the C700 and a Pulse Flex 2i -----------------------------

C700_AUDIO = ("/Settings", frozenset({("id", "audio"), ("schemaVersion", "34")}))


def turned_on(menu: str, setting_id: str) -> str:
    """Return a menu with one of its OFF settings turned ON."""
    return re.sub(
        rf'(<setting id="{setting_id}"[^>]*?value=")OFF"', r'\1ON"', menu, count=1
    )


def test_menu_can_be_a_setting() -> None:
    """The soundbar's subwoofer is a menu that is itself an on/off setting."""
    subwoofer = _parse_settings(soundbar_menu())["subwoofer"]
    assert (subwoofer.url, subwoofer.value) == ("/audiomodes", "default")
    assert subwoofer.options == [("default", "Off"), ("withsub", "On")]


def test_room_name_is_not_kept() -> None:
    """Text settings such as the room name are not read, nor shown in diagnostics."""
    menu = fixture_text(LABEL, MASTER, "settings_player")
    assert "nodename" not in _parse_settings(menu)


async def test_tone_needs_tone_controls(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """On the C700, bass and treble apply only while tone controls are on."""
    await setup_player(hass, MASTER, MASTER_MAC)
    assert hass.states.get("switch.player_1_tone_controls").state == "off"
    assert hass.states.get("number.player_1_bass").state == "unavailable"
    assert hass.states.get("number.player_1_balance").state == "0.0"

    menu = turned_on(fixture_text(LABEL, MASTER, "settings_audio"), "eq-switch")
    patch_session.player(MASTER).overrides[C700_AUDIO] = (200, menu)
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.player_1_tone_controls"},
        blocking=True,
    )

    assert sent(patch_session, "/alsa_setting") == [{"eq-switch": "ON"}]
    assert hass.states.get("number.player_1_bass").state == "0.0"


async def test_c700_settings(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """The C700's subwoofer, output mode and display, as its app shows them."""
    await setup_player(hass, MASTER, MASTER_MAC)

    output = hass.states.get("select.player_1_output_mode")
    assert output.state == "Stereo"
    assert output.attributes["options"] == ["Stereo", "Left", "Right", "Mono"]
    assert hass.states.get("select.player_1_indicator_brightness").state == "Normal"
    assert hass.states.get("switch.player_1_dark_mode").state == "on"
    brightness = hass.states.get("number.player_1_display_brightness")
    assert (brightness.state, brightness.attributes["unit_of_measurement"]) == (
        "60.0",
        "%",
    )

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.player_1_subwoofer"}, blocking=True
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.player_1_output_mode", "option": "Mono"},
        blocking=True,
    )
    assert sent(patch_session, "/audiomodes") == [
        {"subwoofer": "withsub"},
        {"channelMode": "mono"},
    ]


async def test_display_brightness_needs_auto_off(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """While the C700 sets its display brightness itself, the number is unavailable."""
    menu = turned_on(
        fixture_text(LABEL, MASTER, "settings_player"), "auto-lcd-brightness"
    )
    key = ("/Settings", frozenset({("id", "player"), ("schemaVersion", "34")}))
    patch_session.player(MASTER).overrides[key] = (200, menu)
    await setup_player(hass, MASTER, MASTER_MAC)

    state = hass.states.get("number.player_1_display_brightness")
    assert state.state == "unavailable"


async def test_flex_bluetooth_mode(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The Flex's Bluetooth mode comes from its Sources menu."""
    await setup_player(hass, SLAVE_1, SLAVE_1_MAC)

    bluetooth = hass.states.get("select.player_2_bluetooth")
    assert bluetooth.state == "Automatic"
    assert bluetooth.attributes["options"] == [
        "Manual",
        "Automatic",
        "Guest",
        "Disabled",
    ]
    # A single speaker calls its default output Mono, not Stereo.
    assert hass.states.get("select.player_2_output_mode").attributes["options"] == [
        "Mono",
        "Left",
        "Right",
    ]

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.player_2_bluetooth", "option": "Manual"},
        blocking=True,
    )
    assert sent(patch_session, "/audiomodes") == [{"bluetoothAutoplay": "0"}]


@pytest.mark.parametrize("bluos", [BLUOS_418_LABEL], indirect=True)
async def test_absolute_setting_urls(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Settings that name an absolute URL, as BluOS 4.18 does, are changed there.

    They used to be glued onto the player's address, which made an invalid URL.
    """
    await setup_player(hass, BLUOS_418, BLUOS_418_MAC)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.player_1_subwoofer"}, blocking=True
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.player_1_balance", "value": 1},
        blocking=True,
    )

    posts = [(r.raw_url, r.body) for r in patch_session.requests if r.method == "POST"]
    assert posts == [
        ("http://192.0.2.1:11000/audiomodes", {"subwoofer": "withsub"}),
        ("http://192.0.2.1:11000/alsa_setting", {"eq-balance": 1}),
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/audiomodes", "http://player.local:11000/audiomodes"),
        # The player names its own address; the one Home Assistant uses wins.
        (
            "http://192.0.2.1:11000/alsa_setting",
            "http://player.local:11000/alsa_setting",
        ),
        (
            # Port 80 is the default, so it is left out once resolved.
            "http://192.0.2.1:80/wificfg?noheader=1",
            "http://player.local/wificfg?noheader=1",
        ),
    ],
)
def test_setting_url(url: str, expected: str) -> None:
    """Relative and absolute setting URLs both reach the configured player."""
    coordinator = SimpleNamespace(
        host="player.local", _base_url=lambda: "http://player.local:11000"
    )
    assert str(BluesoundCoordinator._setting_url(coordinator, url)) == expected


FLEX_CAPTURE = ("/Settings", frozenset({("id", "capture"), ("schemaVersion", "34")}))
BLUETOOTH = (
    '<item playURL="/Play?url=Capture%3Abluez%3Abluetooth" text="Bluetooth" '
    'image="/images/BluetoothIcon.png" type="audio" inputType="bluetooth"/>'
)


async def test_bluetooth_turned_on_becomes_a_source(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Turning Bluetooth on adds its input, though /Status's sid stays put.

    As on a Pulse Flex 2i on BluOS 4.18.18: the mode change moves /SyncStatus,
    /Browse gains Bluetooth, and a follower's sid is its leader's.
    """
    await setup_player(hass, SLAVE_1, SLAVE_1_MAC)
    player = patch_session.player(SLAVE_1)
    assert "source_list" not in hass.states.get("media_player.player_2").attributes

    capture = player.captured("settings_capture")
    player.overrides[FLEX_CAPTURE] = (
        200,
        re.sub(
            r'(id="bluetoothAutoplay"[^>]*value=")[^"]*"', r'\g<1>0"', capture, count=1
        ),
    )
    browse = player.captured("browse_root").replace(
        "</browse>", f"{BLUETOOTH}</browse>"
    )
    player.overrides[("/Browse", frozenset())] = (200, browse)
    player.push(
        "/SyncStatus",
        player.captured("sync_status").replace('etag="108"', 'etag="109"'),
    )

    await wait_for(
        lambda: (
            hass.states.get("media_player.player_2").attributes.get("source_list")
            == ["Bluetooth"]
        )
    )
