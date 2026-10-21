"""Tests for the media player entity."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from homeassistant.components.media_player import BrowseMedia, MediaPlayerEntityFeature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import async_track_state_change_event
import pytest

from custom_components.bluesound_alt.const import CONF_HIDDEN_SOURCES, DOMAIN
from custom_components.bluesound_alt.media_player import (
    BluesoundMediaPlayer,
    _decode_media_id,
    _encode_media_id,
)

from .conftest import FakeBluOS, setup_player, wait_for
from .const import (
    APPLE_MUSIC_LABEL,
    MASTER,
    MASTER_MAC,
    SLAVE_1,
    SLAVE_1_MAC,
    SLAVE_2,
    SLAVE_2_MAC,
    SOUNDBAR,
    SOUNDBAR_MAC,
    T758,
    T758_LABEL,
    T758_MAC,
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
    # Only connected inputs: Spotify and presets are played from the browser.
    assert master.attributes["source_list"] == ["PlexAmp", "Vinyl"]
    soundbar = hass.states.get("media_player.player_4")
    assert soundbar.attributes["source"] == "HDMI eARC"


async def test_pause_does_not_toggle(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Pause is a plain pause, so it cannot resume an already paused player."""
    await setup_player(hass, MASTER, MASTER_MAC)

    await hass.services.async_call(
        "media_player",
        "media_pause",
        {"entity_id": "media_player.player_1"},
        blocking=True,
    )
    sent = [r for r in patch_session.requests if r.path == "/Pause"]
    assert [r.query for r in sent] == [{}]


async def test_slave_shows_the_group_source(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A slave playing the master's input names it, rather than "Capture"."""
    await setup_group(hass)

    assert hass.states.get("media_player.player_2").attributes["source"] == "PlexAmp"


async def test_sources_follow_the_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Sources and presets are re-read when /Status says they changed.

    The player counts every change to its sources in sid, and to its presets
    in prid, as an input renamed in the BluOS app.
    """
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)
    browse = player.captured("browse_root").replace('text="Vinyl"', 'text="Records"')
    presets = player.captured("presets").replace("P4 Lyden av Norge", "NRK P4")
    player.overrides[("/Browse", frozenset())] = (200, browse)
    player.overrides[("/Presets", frozenset())] = (200, presets)

    status = player.captured("status")
    player.push("/Status", status.replace("<sid>4</sid>", "<sid>5</sid>"))
    await wait_for(
        lambda: (
            hass.states.get("media_player.player_1").attributes["source_list"]
            == ["PlexAmp", "Records"]
        )
    )
    coordinator = entity(hass, "media_player.player_1").coordinator
    assert [p["name"] for p in coordinator.presets] == ["P4 Lyden av Norge"]

    player.push(
        "/Status",
        status.replace("<sid>4</sid>", "<sid>5</sid>").replace(
            "<prid>0</prid>", "<prid>1</prid>"
        ),
    )
    await wait_for(lambda: [p["name"] for p in coordinator.presets] == ["NRK P4"])


async def test_unchanged_status_does_not_reread_sources(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Ordinary playback updates leave the source lists alone."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)
    before = sum(r.path in ("/Browse", "/Presets") for r in patch_session.requests)

    player.push(
        "/Status",
        player.captured("status").replace("<title1>PlexAmp", "<title1>Still PlexAmp"),
    )
    await wait_for(
        lambda: (
            hass.states.get("media_player.player_1").attributes["media_title"]
            == "Still PlexAmp"
        )
    )

    after = sum(r.path in ("/Browse", "/Presets") for r in patch_session.requests)
    assert after == before


def with_external_input(sync_status: str, input_id: str) -> str:
    """Rewrite a captured /SyncStatus as it reads once another input is active."""
    names = dict(re.findall(r'<item id="(\d+)" name="([^"]*)"', sync_status))
    return re.sub(
        r'<externalSource id="\d+" name="[^"]*"',
        f'<externalSource id="{input_id}" name="{names[input_id]}"',
        sync_status,
    )


@pytest.mark.parametrize("bluos", [T758_LABEL], indirect=True)
async def test_receiver_inputs_are_sources(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A receiver's own inputs are listed, and the active one is the source.

    Reported in issue #13: a NAD T758 only offered Bluetooth and Spotify.
    """
    await setup_player(hass, T758, T758_MAC)

    state = hass.states.get("media_player.player_1")
    assert state.attributes["source"] == "Squeeze"
    assert state.attributes["source_list"] == [
        "Bluetooth",
        "BluRay",
        "TV Optical",
        "CD",
        "Squeeze",
        "PS3",
        "BluOS",
    ]


@pytest.mark.parametrize("bluos", [T758_LABEL], indirect=True)
async def test_receiver_input_is_the_title(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """On a receiver input the input is the title, not "External Source".

    Reported in issue #13: cards that show only the title said "External
    Source", and others "External Source - Squeeze".
    """
    await setup_player(hass, T758, T758_MAC)

    state = hass.states.get("media_player.player_1")
    assert state.attributes["media_title"] == "Squeeze"
    assert "media_artist" not in state.attributes


@pytest.mark.parametrize("bluos", [T758_LABEL], indirect=True)
async def test_select_receiver_input(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Selecting a receiver input switches to it by id, and the source follows."""
    await setup_player(hass, T758, T758_MAC)
    player = patch_session.player(T758)

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": "media_player.player_1", "source": "PS3"},
        blocking=True,
    )
    sent = [r for r in patch_session.requests if r.path == "/ExternalSource"]
    assert [r.query for r in sent] == [{"id": "5"}]

    player.push("/SyncStatus", with_external_input(player.captured("sync_status"), "5"))
    await wait_for(
        lambda: hass.states.get("media_player.player_1").attributes["source"] == "PS3"
    )


@pytest.mark.parametrize("bluos", [T758_LABEL], indirect=True)
async def test_receiver_on_bluos_input_is_not_named_bluos(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """With the BluOS module active, its entry is not passed off as the source."""
    await setup_player(hass, T758, T758_MAC)
    player = patch_session.player(T758)

    player.push("/SyncStatus", with_external_input(player.captured("sync_status"), "6"))
    await wait_for(
        lambda: entity(hass, "media_player.player_1").coordinator.external_input == "6"
    )
    assert hass.states.get("media_player.player_1").attributes.get("source") != "BluOS"


async def test_select_unknown_source(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A source the player does not have is refused, not silently ignored."""
    await setup_player(hass, MASTER, MASTER_MAC)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "media_player",
            "select_source",
            {"entity_id": "media_player.player_1", "source": "Cassette"},
            blocking=True,
        )
    assert raised.value.translation_key == "unknown_source"
    assert not [r for r in patch_session.requests if r.path in ("/Play", "/Preset")]


# -- controls ---------------------------------------------------------------


async def test_no_seek_on_an_input(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Seeking is not offered while the player cannot seek, as on an input."""
    await setup_player(hass, MASTER, MASTER_MAC)

    features = hass.states.get("media_player.player_1").attributes["supported_features"]
    assert not features & MediaPlayerEntityFeature.SEEK
    assert features & MediaPlayerEntityFeature.PAUSE


@pytest.mark.parametrize("bluos", [T758_LABEL], indirect=True)
async def test_seek_where_the_player_can(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Seeking is offered while the player reports it can seek."""
    await setup_player(hass, T758, T758_MAC)

    features = hass.states.get("media_player.player_1").attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.SEEK


def offers_sources(hass: HomeAssistant, entity_id: str) -> bool:
    """Return whether a player offers source selection."""
    features = hass.states.get(entity_id).attributes["supported_features"]
    return bool(features & MediaPlayerEntityFeature.SELECT_SOURCE)


async def test_sources_only_with_inputs(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A speaker without inputs offers no source selection; the C700 does."""
    await setup_player(hass, MASTER, MASTER_MAC)
    await setup_player(hass, SLAVE_1, SLAVE_1_MAC)

    assert offers_sources(hass, "media_player.player_1")
    assert not offers_sources(hass, "media_player.player_2")
    assert "source_list" not in hass.states.get("media_player.player_2").attributes


async def test_no_sources_with_every_input_hidden(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Hiding every input takes source selection away, until one is back."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)
    inputs = [key for key, _ in entry.runtime_data.source_options()]

    hass.config_entries.async_update_entry(entry, options={CONF_HIDDEN_SOURCES: inputs})
    await hass.async_block_till_done()
    assert not offers_sources(hass, "media_player.player_1")

    hass.config_entries.async_update_entry(
        entry, options={CONF_HIDDEN_SOURCES: inputs[1:]}
    )
    await hass.async_block_till_done()
    assert offers_sources(hass, "media_player.player_1")


# -- device -----------------------------------------------------------------


async def test_device_maker_is_the_brand(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The device's maker is the player's brand, NAD as well as Bluesound."""
    nad_entry = await setup_player(hass, MASTER, MASTER_MAC)
    soundbar_entry = await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    registry = dr.async_get(hass)
    [nad] = dr.async_entries_for_config_entry(registry, nad_entry.entry_id)
    [soundbar] = dr.async_entries_for_config_entry(registry, soundbar_entry.entry_id)
    assert nad.identifiers == {(DOMAIN, MASTER_MAC)}
    assert (nad.manufacturer, nad.model) == ("NAD", "C700")
    assert (soundbar.manufacturer, soundbar.model) == ("Bluesound", "PULSE SOUNDBAR+")


async def test_device_links_to_the_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The device page links to the player's web page, and knows its MAC."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)

    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.configuration_url == f"http://{MASTER[0]}:80"
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, "00:00:5e:00:53:01")}


async def test_volume_up_is_a_small_step(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Volume up moves 2 steps of 100, not Home Assistant's default 10."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)
    state = hass.states.get("media_player.player_4")
    assert state.attributes["supported_features"] & MediaPlayerEntityFeature.VOLUME_STEP
    before = round(state.attributes["volume_level"] * 100)

    await hass.services.async_call(
        "media_player",
        "volume_up",
        {"entity_id": "media_player.player_4"},
        blocking=True,
    )

    sent = [r for r in patch_session.requests if r.path == "/Volume"]
    assert sent[-1].query == {"level": str(before + 2)}


async def test_clear_queue(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """Clearing the playlist empties the player's queue."""
    await setup_player(hass, MASTER, MASTER_MAC)

    await hass.services.async_call(
        "media_player",
        "clear_playlist",
        {"entity_id": "media_player.player_1"},
        blocking=True,
    )

    assert [r.path for r in patch_session.requests if r.path == "/Clear"] == ["/Clear"]


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

    assert {"PlexAmp", "Radio", "Radio Paradise", "Spotify", "TuneIn"} <= set(children)
    assert children["PlexAmp"].can_play and not children["PlexAmp"].can_expand
    assert children["TuneIn"].can_expand and not children["TuneIn"].can_play
    # Spotify is not a source, so it is played from here, with one tap.
    assert children["Spotify"].can_play and not children["Spotify"].can_expand


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


async def test_presets_are_browsed(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Presets come first in the media browser, each played with one tap."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")

    root = await player.async_browse_media()
    folder = root.children[0]
    assert (folder.title, folder.can_expand, folder.can_play) == (
        "Presets",
        True,
        False,
    )

    presets = await player.async_browse_media(
        folder.media_content_type, folder.media_content_id
    )
    [preset] = presets.children
    assert (preset.title, preset.media_content_id) == (
        "P4 Lyden av Norge",
        "bluesound:preset/1",
    )
    assert preset.can_play and not preset.can_expand
    # A plain-HTTP image, so it comes through Home Assistant.
    assert preset.thumbnail.startswith(PROXY)
    assert image_id(preset) == "http://static.airable.io/43/65/354202.png"


PROXY = "/api/media_player_proxy/media_player.player_1/browse_media/"


def image_id(item: BrowseMedia) -> str:
    """Return the image a proxied thumbnail stands for."""
    return parse_qs(urlsplit(item.thumbnail).query)["media_image_id"][0]


async def test_player_icons_come_through_home_assistant(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The player's own icons, on plain HTTP, are passed through Home Assistant.

    An HTTPS frontend, or a phone away from home, cannot load them from the
    player, so the top level showed no icons.
    """
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")
    patch_session.player(MASTER).overrides[
        ("/Sources/images/SpotifyIcon.png", frozenset())
    ] = (200, "spotify-png", "image/png")

    spotify = children_named(await player.async_browse_media())["Spotify"]
    assert spotify.thumbnail.startswith(PROXY)
    assert image_id(spotify) == "/Sources/images/SpotifyIcon.png"

    image = await player.async_get_browse_image(
        spotify.media_content_type, spotify.media_content_id, image_id(spotify)
    )
    assert image == (b"spotify-png", "image/png")


async def test_https_images_load_directly(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Images already on HTTPS, such as TuneIn's, are left as they are."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")

    tunein = children_named(await player.async_browse_media())["TuneIn"]
    menu = await player.async_browse_media(
        tunein.media_content_type, tunein.media_content_id
    )
    assert children_named(menu)["For You"].thumbnail == (
        "https://cdn-profiles.tunein.com/RootMenu/Logo_754o.png?t=1694533369000"
    )


async def test_only_images_handed_out_are_fetched(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The image proxy fetches nothing it did not offer while browsing."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")
    before = len(patch_session.requests)

    for asked in ("/Sources/images/SpotifyIcon.png", "http://192.0.2.9/secret"):
        image = await player.async_get_browse_image("music", "bluesound:root", asked)
        assert image == (None, None)
    assert len(patch_session.requests) == before


async def test_play_preset(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A preset plays by its id, as an automation would ask for it."""
    await setup_player(hass, MASTER, MASTER_MAC)

    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": "media_player.player_1",
            "media_content_type": "music",
            "media_content_id": "bluesound:preset/1",
        },
        blocking=True,
    )

    sent = [r for r in patch_session.requests if r.path == "/Preset"]
    assert [r.query for r in sent] == [{"id": "1"}]


async def test_play_unknown_preset(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A preset the player does not have is refused, not sent."""
    await setup_player(hass, MASTER, MASTER_MAC)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": "media_player.player_1",
                "media_content_type": "music",
                "media_content_id": "bluesound:preset/9",
            },
            blocking=True,
        )
    assert raised.value.translation_key == "unknown_preset"
    assert not [r for r in patch_session.requests if r.path == "/Preset"]


async def test_preset_still_selectable_by_name(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Automations that select a preset as a source keep working."""
    await setup_player(hass, MASTER, MASTER_MAC)

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": "media_player.player_1", "source": "P4 Lyden av Norge"},
        blocking=True,
    )

    sent = [r for r in patch_session.requests if r.path == "/Preset"]
    assert [r.query for r in sent] == [{"id": "1"}]


async def test_spotify_still_selectable_by_name(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Automations that select Spotify as a source keep working."""
    await setup_player(hass, MASTER, MASTER_MAC)

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": "media_player.player_1", "source": "Spotify"},
        blocking=True,
    )

    sent = [r for r in patch_session.requests if r.path == "/Play"]
    assert [r.query for r in sent] == [{"url": "Spotify:play"}]


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


# -- volume -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("players", "entity_id"),
    [
        ([(SOUNDBAR, SOUNDBAR_MAC)], "media_player.player_4"),
        ([(MASTER, MASTER_MAC)], "media_player.player_1"),
        ([(MASTER, MASTER_MAC), (SLAVE_1, SLAVE_1_MAC)], "media_player.player_2"),
    ],
    ids=["standalone", "leader", "follower"],
)
async def test_volume_change_does_not_flash_back(
    hass: HomeAssistant,
    patch_session: FakeBluOS,
    players: list,
    entity_id: str,
) -> None:
    """The slider stays where it was set, without jumping back in between.

    Reported from real use: the slider flashed back to the old level for a
    moment before settling where it was dragged.
    """
    for player, mac in players:
        await setup_player(hass, player, mac)
    await hass.async_block_till_done()
    before = hass.states.get(entity_id).attributes["volume_level"]
    levels: list[float] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        if new_state := event.data["new_state"]:
            levels.append(new_state.attributes.get("volume_level"))

    async_track_state_change_event(hass, [entity_id], record)
    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": entity_id, "volume_level": 0.2},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert before != 0.2
    assert levels, "the new level was never shown"
    assert all(level == 0.2 for level in levels), levels


# -- Apple Music --------------------------------------------------------------


@pytest.mark.parametrize("bluos", [APPLE_MUSIC_LABEL], indirect=True)
async def test_apple_music_playing(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Apple Music shows as the source, with the track, on the whole group."""
    await setup_group(hass)

    for member in (
        "media_player.player_1",
        "media_player.player_2",
        "media_player.player_3",
    ):
        state = hass.states.get(member)
        assert state.state == "playing"
        assert state.attributes["source"] == "Apple Music"
        assert state.attributes["media_title"] == "GEEKALEEK (feat. Cash Kidd)"
        assert state.attributes["media_artist"] == "OHGEESY"
        assert state.attributes["media_album_name"] == "GEEZYWORLD 2"
        assert state.attributes["media_duration"] == 146
        assert state.attributes["supported_features"] & MediaPlayerEntityFeature.SEEK

    # Artwork comes from the player, as a path on its own address.
    assert entity(hass, "media_player.player_1").media_image_url.startswith(
        "http://192.0.2.1:11000/Artwork?service=AppleMusic"
    )


@pytest.mark.parametrize("bluos", [APPLE_MUSIC_LABEL], indirect=True)
async def test_apple_music_is_browsed_not_a_source(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Apple Music is a menu in the media browser, as TuneIn is, not a source."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = entity(hass, "media_player.player_1")

    assert (
        "Apple Music"
        not in hass.states.get("media_player.player_1").attributes["source_list"]
    )

    apple = children_named(await player.async_browse_media())["Apple Music"]
    assert apple.can_expand and not apple.can_play
    menu = await player.async_browse_media(
        apple.media_content_type, apple.media_content_id
    )
    assert list(children_named(menu)) == [
        "Playlists Made for You",
        "Recently Played",
        "Popular",
        "Genres",
        "Radio",
        "Library",
    ]
