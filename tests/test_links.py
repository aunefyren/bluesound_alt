"""Tests for linked inputs: another media player standing in for an input.

The captured C700 plays its PlexAmp input, a coaxial feed from a computer
whose own player is in Home Assistant. Linked to that player, the C700 shows
its track, artwork and state while the input plays, and passes it the
playback controls. Volume, sources and grouping stay the C700's.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.bluesound_alt.const import CONF_LINKED_PLAYERS
from custom_components.bluesound_alt.media_player import BluesoundMediaPlayer

from .conftest import FakeBluOS, setup_player, wait_for
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

C700 = "media_player.player_1"
CALDERA = "media_player.caldera"
PLEXAMP = "browse:Capture:hw:imxspdif,0/1/25/2?id=input2"
VINYL_STREAM = "Capture:plughw:imxnadadc,0/44100/24/2?id=input0"
PICTURE = "/api/media_player_proxy/media_player.caldera?token=abc&cache=1"


def caldera(hass: HomeAssistant, state: str = "playing", **attributes) -> None:
    """Set the linked player's state, as its own integration would."""
    hass.states.async_set(
        CALDERA,
        state,
        {
            "media_title": "Teardrop",
            "media_artist": "Massive Attack",
            "media_album_name": "Mezzanine",
            "media_duration": 330,
            "media_position": 42,
            "media_position_updated_at": dt_util.utcnow(),
            "entity_picture": PICTURE,
            "supported_features": MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.SEEK,
            **attributes,
        },
    )


async def linked_c700(hass: HomeAssistant, target: str = CALDERA):
    """Set up the C700 with its PlexAmp input linked."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)
    hass.config_entries.async_update_entry(
        entry, options={CONF_LINKED_PLAYERS: {PLEXAMP: target}}
    )
    await hass.async_block_till_done()
    return entry


def entity(hass: HomeAssistant, entity_id: str) -> BluesoundMediaPlayer:
    """Return the entity object behind an entity id."""
    component: EntityComponent = hass.data["entity_components"]["media_player"]
    found = component.get_entity(entity_id)
    assert isinstance(found, BluesoundMediaPlayer)
    return found


async def test_linked_player_stands_in(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """While the input plays, its track, artwork and state are the linked one's."""
    caldera(hass)
    await linked_c700(hass)
    await wait_for(
        lambda: hass.states.get(C700).attributes.get("media_title") == "Teardrop"
    )

    state = hass.states.get(C700)
    assert state.state == "playing"
    assert state.attributes["media_artist"] == "Massive Attack"
    assert state.attributes["media_album_name"] == "Mezzanine"
    assert state.attributes["media_duration"] == 330
    assert state.attributes["media_position"] == 42
    assert state.attributes["entity_picture"] == PICTURE
    assert state.attributes["linked_media_player"] == CALDERA
    # The C700 keeps its own volume and input.
    assert state.attributes["source"] == "PlexAmp"
    assert state.attributes["volume_level"] == entity(hass, C700).volume_level
    assert state.attributes["supported_features"] & MediaPlayerEntityFeature.SEEK


async def test_follows_the_linked_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A new track or a pause on the linked player shows straight away."""
    caldera(hass)
    await linked_c700(hass)

    caldera(hass, "paused", media_title="Angel")
    await wait_for(
        lambda: hass.states.get(C700).attributes.get("media_title") == "Angel"
    )
    assert hass.states.get(C700).state == "paused"


async def test_idle_linked_player_does_not_stand_in(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """With nothing on the linked player, the C700 shows its own input again."""
    caldera(hass)
    await linked_c700(hass)

    caldera(hass, "idle")
    await wait_for(
        lambda: "linked_media_player" not in hass.states.get(C700).attributes
    )
    assert hass.states.get(C700).attributes["media_title"] == "PlexAmp"


async def test_other_input_is_not_linked(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Switched to another input, the C700 is itself."""
    caldera(hass)
    await linked_c700(hass)
    player = patch_session.player(MASTER)

    player.push(
        "/Status",
        player.captured("status_long_poll")
        .replace("Capture:hw:imxspdif,0/1/25/2?id=input2", VINYL_STREAM)
        .replace("<title1>PlexAmp</title1>", "<title1>Vinyl</title1>"),
    )
    await wait_for(lambda: hass.states.get(C700).attributes.get("source") == "Vinyl")
    assert "linked_media_player" not in hass.states.get(C700).attributes
    assert hass.states.get(C700).attributes["media_title"] == "Vinyl"


async def test_controls_go_to_the_linked_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Play, pause, skip and seek reach the linked player, not the C700."""
    caldera(hass)
    await linked_c700(hass)
    player = entity(hass, C700)
    calls = {
        service: async_mock_service(hass, "media_player", service)
        for service in ("media_pause", "media_play", "media_next_track", "media_seek")
    }

    await player.async_media_pause()
    await player.async_media_play()
    await player.async_media_next_track()
    await player.async_media_seek(100)

    for service, received in calls.items():
        assert [c.data["entity_id"] for c in received] == [CALDERA], service
    assert calls["media_seek"][0].data["seek_position"] == 100
    sent = {r.path for r in patch_session.requests}
    assert not {"/Pause", "/Play", "/Skip"} & sent


async def test_group_follows_its_leaders_link(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Speakers playing the C700's input show the linked player's track too."""
    caldera(hass)
    await linked_c700(hass)
    await setup_player(hass, SLAVE_1, SLAVE_1_MAC)
    await setup_player(hass, SLAVE_2, SLAVE_2_MAC)

    for follower in ("media_player.player_2", "media_player.player_3"):
        await wait_for(
            lambda f=follower: (
                hass.states.get(f).attributes.get("media_title") == "Teardrop"
            )
        )


async def test_link_to_another_bluesound_player_is_ignored(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Two of our players linked to each other would pass commands in circles."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)
    await linked_c700(hass, target="media_player.player_4")

    assert hass.states.get("media_player.player_4").state == "playing"
    assert "linked_media_player" not in hass.states.get(C700).attributes
    assert hass.states.get(C700).attributes["media_title"] == "PlexAmp"


# -- browsing a linked input ---------------------------------------------------


class FakeCaldera(MediaPlayerEntity):
    """A media player with a library to browse, as Plex's are."""

    _attr_name = "Caldera"
    _attr_state = MediaPlayerState.PLAYING
    _attr_supported_features = (
        MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.PAUSE
    )

    def __init__(self) -> None:
        """Start with nothing played."""
        self.played: list[tuple[str, str]] = []

    async def async_browse_media(
        self, media_content_type: str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        """Return Plex, Music, then an album."""
        if media_content_id is None:
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id="plex://root",
                media_content_type="plex",
                title="Plex",
                can_play=False,
                can_expand=True,
                can_search=True,
                children=[
                    BrowseMedia(
                        media_class=MediaClass.DIRECTORY,
                        media_content_id="plex://music",
                        media_content_type="library",
                        title="Music",
                        can_play=False,
                        can_expand=True,
                    )
                ],
            )
        if (media_content_type, media_content_id) == ("library", "plex://music"):
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id="plex://music",
                media_content_type="library",
                title="Music",
                can_play=False,
                can_expand=True,
                children=[
                    BrowseMedia(
                        media_class=MediaClass.ALBUM,
                        media_content_id="plex://album/1",
                        media_content_type="album",
                        title="Mezzanine",
                        can_play=True,
                        can_expand=False,
                        thumbnail=ALBUM_ART,
                    )
                ],
            )
        raise BrowseError(f"no {media_content_type} {media_content_id}")

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        """Note what was played."""
        self.played.append((media_type, media_id))


ALBUM_ART = "/api/media_player_proxy/media_player.caldera/browse_media/album/1"


async def linked_to_caldera(hass: HomeAssistant) -> FakeCaldera:
    """Set up the C700 with PlexAmp linked to a Caldera that can be browsed."""
    await linked_c700(hass)
    caldera_player = FakeCaldera()
    await hass.data[DATA_INSTANCES]["media_player"].async_add_entities([caldera_player])
    await hass.async_block_till_done()
    assert caldera_player.entity_id == CALDERA
    return caldera_player


def children_named(media: BrowseMedia) -> dict[str, BrowseMedia]:
    """Index a browse result's children by title."""
    return {child.title: child for child in media.children or []}


async def test_linked_input_opens_its_players_media(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A linked input is a folder of its player's media, and still switches."""
    await linked_to_caldera(hass)
    player = entity(hass, C700)

    root = children_named(await player.async_browse_media())
    plexamp, vinyl = root["PlexAmp"], root["Vinyl"]
    assert plexamp.can_expand and plexamp.can_play
    assert vinyl.can_play and not vinyl.can_expand

    plex = await player.async_browse_media(
        plexamp.media_content_type, plexamp.media_content_id
    )
    assert plex.title == "Plex"
    assert not plex.can_search
    music = children_named(plex)["Music"]
    assert music.media_content_id.startswith("bluesound:linked?")

    library = await player.async_browse_media(
        music.media_content_type, music.media_content_id
    )
    album = children_named(library)["Mezzanine"]
    assert album.can_play and not album.can_expand
    assert album.thumbnail == ALBUM_ART


async def test_play_from_a_linked_input(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Playing an item switches to the input, then the linked player plays it."""
    caldera_player = await linked_to_caldera(hass)
    player = entity(hass, C700)
    plex = await player.async_browse_media(
        "music",
        children_named(await player.async_browse_media())["PlexAmp"].media_content_id,
    )
    music = children_named(plex)["Music"]
    library = await player.async_browse_media(
        music.media_content_type, music.media_content_id
    )
    album = children_named(library)["Mezzanine"]

    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": C700,
            "media_content_type": album.media_content_type,
            "media_content_id": album.media_content_id,
        },
        blocking=True,
    )

    sent = [r.query for r in patch_session.requests if r.path == "/Play"]
    assert sent == [{"url": "Capture:hw:imxspdif,0/1/25/2?id=input2"}]
    assert caldera_player.played == [("album", "plex://album/1")]


async def test_play_a_linked_input_itself(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The linked input's own play button only switches to it."""
    caldera_player = await linked_to_caldera(hass)
    player = entity(hass, C700)
    plexamp = children_named(await player.async_browse_media())["PlexAmp"]

    await player.async_play_media(plexamp.media_content_type, plexamp.media_content_id)

    sent = [r.query for r in patch_session.requests if r.path == "/Play"]
    assert sent == [{"url": "Capture:hw:imxspdif,0/1/25/2?id=input2"}]
    assert caldera_player.played == []


async def test_link_without_a_library_stays_a_switch(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A linked player that cannot be browsed leaves the input as it was."""
    caldera(hass)
    await linked_c700(hass)

    root = children_named(await entity(hass, C700).async_browse_media())
    assert root["PlexAmp"].can_play and not root["PlexAmp"].can_expand


async def test_item_of_a_removed_link(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """An item picked before its link was removed is refused, not half played."""
    await linked_to_caldera(hass)
    player = entity(hass, C700)
    plexamp = children_named(await player.async_browse_media())["PlexAmp"]
    plex = await player.async_browse_media(
        plexamp.media_content_type, plexamp.media_content_id
    )
    music = children_named(plex)["Music"]
    hass.config_entries.async_update_entry(
        player._entry, options={CONF_LINKED_PLAYERS: {}}
    )
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await player.async_play_media(music.media_content_type, music.media_content_id)
    assert raised.value.translation_key == "not_linked"
    assert not [r for r in patch_session.requests if r.path == "/Play"]
