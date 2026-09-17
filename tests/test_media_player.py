"""Tests for the media player entity."""

from __future__ import annotations

from homeassistant.components.media_player import BrowseMedia
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.bluesound_alt.media_player import (
    BluesoundMediaPlayer,
    _decode_media_id,
    _encode_media_id,
)

from .conftest import FakeBluOS, setup_player
from .const import (
    MASTER,
    MASTER_MAC,
    SLAVE_1,
    SLAVE_1_MAC,
    SLAVE_2,
    SLAVE_2_MAC,
    SOUNDBAR,
    SOUNDBAR_MAC,
)


def entity(hass: HomeAssistant, entity_id: str) -> BluesoundMediaPlayer:
    """Return the entity object behind an entity id."""
    component: EntityComponent = hass.data["entity_components"]["media_player"]
    found = component.get_entity(entity_id)
    assert isinstance(found, BluesoundMediaPlayer)
    return found


async def setup_group(hass: HomeAssistant) -> None:
    """Set up the captured group: the master and both slaves."""
    await setup_player(hass, MASTER, MASTER_MAC)
    await setup_player(hass, SLAVE_1, SLAVE_1_MAC)
    await setup_player(hass, SLAVE_2, SLAVE_2_MAC)


# -- sources ----------------------------------------------------------------


async def test_source_is_the_playing_input(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The source is the input whose stream is playing."""
    await setup_player(hass, MASTER, MASTER_MAC)
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    master = hass.states.get("media_player.player_1")
    assert master.attributes["source"] == "PlexAmp"
    assert master.attributes["source_list"] == [
        "PlexAmp",
        "Vinyl",
        "Spotify",
        "P4 Lyden av Norge",
    ]
    soundbar = hass.states.get("media_player.player_4")
    assert soundbar.attributes["source"] == "HDMI eARC"


async def test_slave_shows_the_group_source(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A slave playing the master's input names it, rather than "Capture"."""
    await setup_group(hass)

    assert hass.states.get("media_player.player_2").attributes["source"] == "PlexAmp"


# -- grouping ---------------------------------------------------------------


async def test_group_members(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """Every member of a group reports the whole group, master first."""
    await setup_group(hass)
    group = ["media_player.player_1", "media_player.player_2", "media_player.player_3"]

    for member in group:
        assert hass.states.get(member).attributes["group_members"] == group


async def test_standalone_has_no_group(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A standalone player is in no group."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    assert hass.states.get("media_player.player_4").attributes.get(
        "group_members", []
    ) in ([], None)


# -- browsing ---------------------------------------------------------------


def children_named(media: BrowseMedia) -> dict[str, BrowseMedia]:
    """Index a browse result's children by title."""
    return {child.title: child for child in media.children or []}


async def test_browse_root(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """The top level lists the player's inputs and services."""
    await setup_player(hass, MASTER, MASTER_MAC)

    root = await entity(hass, "media_player.player_1").async_browse_media()
    children = children_named(root)

    assert {"PlexAmp", "Radio", "Radio Paradise", "TuneIn"} <= set(children)
    assert children["PlexAmp"].can_play and not children["PlexAmp"].can_expand
    assert children["TuneIn"].can_expand and not children["TuneIn"].can_play


async def test_browse_categorised_menu(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Radio Paradise groups its stations into categories; all are listed."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")

    root = await player.async_browse_media()
    paradise = children_named(root)["Radio Paradise"]
    menu = await player.async_browse_media(
        paradise.media_content_type, paradise.media_content_id
    )

    assert len(menu.children) == 14
    assert all(child.can_play for child in menu.children)


async def test_play_browsed_station(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Playing a browsed station sends its play URL exactly as given."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")

    root = await player.async_browse_media()
    radio = children_named(root)["Radio"]
    menu = await player.async_browse_media(
        radio.media_content_type, radio.media_content_id
    )
    charts = children_named(menu)["Most popular stations"]
    stations = await player.async_browse_media(
        charts.media_content_type, charts.media_content_id
    )
    station = children_named(stations)["NRK P1+"]

    await player.async_play_media(station.media_content_type, station.media_content_id)

    _, play_url = _decode_media_id(station.media_content_id)
    sent = [r for r in patch_session.requests if r.path == "/Play"]
    assert sent[-1].raw_url == f"http://{MASTER[0]}:{MASTER[1]}{play_url}"


def test_media_id_round_trip() -> None:
    """Browse keys and play URLs survive being packed into a media id."""
    browse_key = (
        "TuneIn:BrowseMenu/%2FRadioBrowse%3Fservice=TuneIn&url=https%253A%252F%252F"
        "api.radiotime.com%252Fcategories%252Fhome%253Fserial%253DREDACTED"
    )
    play_url = "/Play?url=Airable%3Aradio%3Ahttps&title=NRK+P1%2B"

    assert _decode_media_id(_encode_media_id(browse_key, play_url)) == (
        browse_key,
        play_url,
    )
    assert _decode_media_id(_encode_media_id(browse_key, None)) == (browse_key, None)
    assert _decode_media_id(_encode_media_id(None, play_url)) == (None, play_url)
