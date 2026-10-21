"""Bluesound Alt media player entity."""
from __future__ import annotations

from collections import OrderedDict
import contextlib
import copy
from datetime import datetime
import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ALBUM_NAME,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_POSITION_UPDATED_AT,
    ATTR_MEDIA_SEEK_POSITION,
    ATTR_MEDIA_TITLE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ENTITY_PICTURE,
    ATTR_SUPPORTED_FEATURES,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from . import BluesoundConfigEntry
from .const import CONF_HIDDEN_SOURCES, CONF_LINKED_PLAYERS, DOMAIN
from .coordinator import BluesoundCoordinator
from .grouping import async_join, async_unjoin

_LOGGER = logging.getLogger(__name__)

SUPPORT_BLUESOUND = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
)

_STATE_MAP: dict[str, MediaPlayerState] = {
    "play": MediaPlayerState.PLAYING,
    "stream": MediaPlayerState.PLAYING,
    "pause": MediaPlayerState.PAUSED,
    "stop": MediaPlayerState.IDLE,
    "connecting": MediaPlayerState.BUFFERING,
}

# BluOS /Repeat?state=<n>: 0 = repeat queue, 1 = repeat track, 2 = repeat off
_REPEAT_TO_HA: dict[int, RepeatMode] = {
    0: RepeatMode.ALL,
    1: RepeatMode.ONE,
    2: RepeatMode.OFF,
}
_HA_TO_REPEAT: dict[RepeatMode, int] = {v: k for k, v in _REPEAT_TO_HA.items()}

# Friendly labels for push inputs that have no /Browse source entry.
# AirPlay self-labels via the BluOS serviceName field, so only "http" needs mapping.
_SERVICE_LABELS: dict[str, str] = {"http": "Streaming"}

# What a receiver reports as its stream while one of its own inputs plays.
_EXTERNAL_STREAM = "Capture:External"

# Media browser: media_content_id encodes a BluOS browseKey (b) and/or playURL (p).
_BLUESOUND_SCHEME = "bluesound:"
_BLUESOUND_ROOT = "bluesound:root"
# The player's presets, as a folder of their own, and one preset by its id:
# bluesound:preset/3, short enough to write in an automation by hand.
_BLUESOUND_PRESETS = "bluesound:presets"
_BLUESOUND_PRESET = "bluesound:preset/"
# A linked input's media player, browsed through this one: the input (i) and,
# below its top level, the linked player's own content type (t) and id (c).
_BLUESOUND_LINKED = "bluesound:linked"
# Browse images not on HTTPS, such as the player's own icons, are passed
# through Home Assistant: an HTTPS frontend, or a phone away from home, cannot
# load them from the player. Only images handed out while browsing are
# fetched, the most recent this many.
_MAX_BROWSE_IMAGES = 1000

# States in which a linked media player stands in for the player.
_LINK_ACTIVE = {
    MediaPlayerState.PLAYING,
    MediaPlayerState.PAUSED,
    MediaPlayerState.BUFFERING,
}

# Sent when a player entity comes or goes. Group membership is resolved
# through the other players' entities, so theirs may have changed too.
_SIGNAL_PLAYERS_CHANGED = f"{DOMAIN}_players_changed"


def _encode_media_id(browse_key: str | None, play_url: str | None) -> str:
    """Encode a BluOS browseKey/playURL pair into a media_content_id."""
    parts: list[tuple[str, str]] = []
    if browse_key is not None:
        parts.append(("b", browse_key))
    if play_url is not None:
        parts.append(("p", play_url))
    return f"{_BLUESOUND_SCHEME}?{urlencode(parts)}"


def _encode_linked(
    input_key: str, content_type: str | None = None, content_id: str | None = None
) -> str:
    """Encode a linked input, or an item of its media player, as a media id."""
    parts = [("i", input_key)]
    if content_id is not None:
        parts += [("t", content_type or ""), ("c", content_id)]
    return f"{_BLUESOUND_LINKED}?{urlencode(parts)}"


def _decode_linked(content_id: str) -> tuple[str, str | None, str | None]:
    """Return (input key, content type, content id) from a linked media id."""
    qs = parse_qs(urlsplit(content_id).query, keep_blank_values=True)
    return (
        qs["i"][0],
        qs["t"][0] if "t" in qs else None,
        qs["c"][0] if "c" in qs else None,
    )


def _decode_media_id(content_id: str) -> tuple[str | None, str | None]:
    """Return (browse_key, play_url) decoded from a media_content_id."""
    qs = parse_qs(urlsplit(content_id).query, keep_blank_values=True)
    browse_key = qs["b"][0] if "b" in qs else None
    play_url = qs["p"][0] if "p" in qs else None
    return browse_key, play_url


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluesoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the media player for a configured player."""
    async_add_entities([BluesoundMediaPlayer(entry.runtime_data, entry)])


class BluesoundMediaPlayer(CoordinatorEntity[BluesoundCoordinator], MediaPlayerEntity):
    """Representation of a Bluesound player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_media_position_updated_at = None
    # Volume up and down move 2 of the player's 100 steps. Home Assistant's
    # default, 10, jumps too far for a speaker.
    _attr_volume_step = 0.02

    def __init__(self, coordinator: BluesoundCoordinator, entry: ConfigEntry) -> None:
        """Set up the entity for one player."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = coordinator.sync_info.mac
        self._attr_device_info = coordinator.device_info
        self._optimistic_volume: float | None = None
        self._optimistic_muted: bool | None = None
        self._browse_images: OrderedDict[str, None] = OrderedDict()
        # The linked media players being followed, and how to stop.
        self._link_targets: frozenset[str] = frozenset()
        self._unsub_links: CALLBACK_TYPE | None = None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return the controls the player offers.

        Seeking only where it can, and source selection only with inputs to
        choose from: a speaker without any, or with all of them hidden, has
        none.
        """
        features = SUPPORT_BLUESOUND
        if (linked := self._linked_state()) is not None:
            linked_features = linked.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
            if linked_features & MediaPlayerEntityFeature.SEEK:
                features |= MediaPlayerEntityFeature.SEEK
        elif self.coordinator.data is not None and self.coordinator.data.can_seek:
            features |= MediaPlayerEntityFeature.SEEK
        if not self.source_list:
            features &= ~MediaPlayerEntityFeature.SELECT_SOURCE
        return features

    @property
    def state(self) -> MediaPlayerState | None:
        """Return whether the player is playing, paused or idle."""
        if (linked := self._linked_state()) is not None:
            return MediaPlayerState(linked.state)
        return _STATE_MAP.get(self.coordinator.data.state)

    @property
    def volume_level(self) -> float | None:
        """Return this player's own volume, also when grouped."""
        if self._optimistic_volume is not None:
            return self._optimistic_volume
        individual = self.coordinator.individual_volume
        if self.coordinator.group_master_ip and individual is not None:
            return individual / 100
        return self.coordinator.data.volume / 100

    @property
    def is_volume_muted(self) -> bool:
        """Return whether the player is muted."""
        if self._optimistic_muted is not None:
            return self._optimistic_muted
        return self.coordinator.data.muted

    @property
    def shuffle(self) -> bool:
        """Return whether shuffle is on."""
        return self.coordinator.data.shuffle

    @property
    def repeat(self) -> RepeatMode | None:
        """Return the repeat mode."""
        return _REPEAT_TO_HA.get(self.coordinator.data.repeat)

    @property
    def media_title(self) -> str | None:
        """Return the title of what is playing.

        On a receiver input the player reports the generic "External Source"
        as the title and the input as the artist, so cards that show only the
        title would never say which input is on. The input is the title then.
        """
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_MEDIA_TITLE)
        data = self.coordinator.data
        if self._playing_external_input():
            return data.artist or self._active_external_input() or data.title
        return data.title

    @property
    def media_artist(self) -> str | None:
        """Return the artist of what is playing."""
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_MEDIA_ARTIST)
        if self._playing_external_input():
            return None
        return self.coordinator.data.artist

    def _playing_external_input(self) -> bool:
        """Return whether one of the receiver's own inputs is playing."""
        stream_url = self.coordinator.data.stream_url
        return bool(stream_url and stream_url.startswith(_EXTERNAL_STREAM))

    @property
    def media_album_name(self) -> str | None:
        """Return the album of what is playing."""
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_MEDIA_ALBUM_NAME)
        return self.coordinator.data.album

    @property
    def media_image_url(self) -> str | None:
        """Return the artwork URL, made absolute."""
        img = self.coordinator.data.image_url
        if img and not img.startswith("http"):
            return f"http://{self.coordinator.host}:{self.coordinator.port}{img}"
        return img

    @property
    def media_position(self) -> float | None:
        """Return how far into the track playback is, in seconds."""
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_MEDIA_POSITION)
        return self.coordinator.data.position

    @property
    def media_position_updated_at(self) -> datetime | None:
        """Return when the position was last reported."""
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_MEDIA_POSITION_UPDATED_AT)
        return self._attr_media_position_updated_at

    @property
    def media_duration(self) -> float | None:
        """Return the length of the track, in seconds."""
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_MEDIA_DURATION)
        return self.coordinator.data.duration

    @property
    def entity_picture(self) -> str | None:
        """Return the artwork, the linked media player's while it stands in.

        Its picture is already served by Home Assistant, so it is passed on
        as it is.
        """
        if (linked := self._linked_state()) is not None:
            return linked.attributes.get(ATTR_ENTITY_PICTURE)
        return super().entity_picture

    @property
    def source_list(self) -> list[str]:
        """Return the audio inputs connected to the player.

        Services and presets are played from the media browser. Inputs hidden
        in the player's options are left out. Either still shows as the
        source while it plays.
        """
        hidden = set(self._entry.options.get(CONF_HIDDEN_SOURCES, []))
        return [
            name
            for key, name in self.coordinator.source_options()
            if key not in hidden
        ]

    @property
    def source(self) -> str | None:
        """Return the input, service or preset that is playing."""
        stream_url = self.coordinator.data.stream_url
        if stream_url:
            for s in self._playable_sources():
                if s["play_url"] == stream_url:
                    return s["name"]
            if self._playing_external_input() and (
                name := self._active_external_input()
            ):
                return name
        # Push inputs (AirPlay, URL/streaming) match no /Browse source, fall
        # back to the active BluOS service so the UI shows what is playing.
        data = self.coordinator.data
        if not data.service:
            return None
        return _SERVICE_LABELS.get(data.service, data.service_name or data.service)

    def _active_external_input(self) -> str | None:
        """Name the receiver input that is playing, this player's or its master's.

        The BluOS module's own entry is left out: while it is active, the
        stream is one of the module's sources, and that is the better name.
        """
        coordinators = [self.coordinator]
        master_ip = self.coordinator.group_master_ip
        if master_ip and (master := self._find_coordinator_by_ip(master_ip)):
            coordinators.append(master)
        for coordinator in coordinators:
            for i in coordinator.external_inputs:
                if i["id"] == coordinator.external_input and not i["bluos"]:
                    return i["name"]
        return None

    def _playable_sources(self) -> list[dict[str, str]]:
        """Return the sources whose stream this player may be reporting.

        A slave reports whatever the group plays, which is usually one of the
        master's inputs rather than one of its own.
        """
        sources = self.coordinator.sources
        master_ip = self.coordinator.group_master_ip
        if master_ip and (master := self._find_coordinator_by_ip(master_ip)):
            return [*sources, *master.sources]
        return sources

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the group members, and the media player standing in."""
        attributes: dict[str, Any] = {"bluesound_group": self.group_members}
        if (linked := self._linked_state()) is not None:
            attributes["linked_media_player"] = linked.entity_id
        return attributes

    # --- Linked inputs ---

    def _active_input(self) -> tuple[BluesoundCoordinator, str] | None:
        """Return the input playing, as the player it belongs to and its key.

        A player in a group plays its leader's input, so that is looked for
        among the leader's inputs too. The key is the one the options use.
        """
        data = self.coordinator.data
        players = [self.coordinator]
        master_ip = self.coordinator.group_master_ip
        if master_ip and (master := self._find_coordinator_by_ip(master_ip)):
            players.append(master)
        if data.stream_url:
            for player in players:
                for s in player.sources:
                    if s["play_url"] == data.stream_url:
                        return player, f"browse:{s['play_url']}"
        if self._playing_external_input():
            for player in players:
                for i in player.external_inputs:
                    if i["id"] == player.external_input and not i["bluos"]:
                        return player, f"input:{i['id']}"
        return None

    def _linked_state(self) -> State | None:
        """Return the linked media player's state, while it stands in.

        That is while its input plays and it has something on. Links to
        other Bluesound Alt players are ignored, so two players linked to
        each other cannot pass commands back and forth.
        """
        if self.hass is None or (active := self._active_input()) is None:
            return None
        player, key = active
        if (entity_id := self._link_target(player.config_entry.options, key)) is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state not in _LINK_ACTIVE:
            return None
        return state

    def _link_target(self, options: Any, key: str) -> str | None:
        """Return the media player an input is linked to, if the link counts.

        Links to other Bluesound Alt players do not: two players linked to
        each other would pass commands back and forth.
        """
        entity_id = options.get(CONF_LINKED_PLAYERS, {}).get(key)
        if not entity_id or entity_id == self.entity_id:
            return None
        registered = er.async_get(self.hass).async_get(entity_id)
        if registered is not None and registered.platform == DOMAIN:
            return None
        return entity_id

    def _browsable_link(self, key: str) -> MediaPlayerEntity | None:
        """Return the media player linked to one of this player's inputs.

        Only one that can be browsed, as its media list is shown in place of
        the input's.
        """
        if (entity_id := self._link_target(self._entry.options, key)) is None:
            return None
        component = self.hass.data.get(DATA_INSTANCES, {}).get(MEDIA_PLAYER_DOMAIN)
        entity = component.get_entity(entity_id) if component else None
        if entity is None or not (
            entity.supported_features & MediaPlayerEntityFeature.BROWSE_MEDIA
        ):
            return None
        return entity

    @callback
    def _follow_links(self) -> None:
        """Follow the media players linked to this player's or its leader's inputs."""
        players = [self.coordinator]
        master_ip = self.coordinator.group_master_ip
        if master_ip and (master := self._find_coordinator_by_ip(master_ip)):
            players.append(master)
        targets = frozenset(
            entity_id
            for player in players
            for entity_id in player.config_entry.options.get(
                CONF_LINKED_PLAYERS, {}
            ).values()
        )
        if targets == self._link_targets:
            return
        self._stop_following_links()
        self._link_targets = targets
        if targets:
            self._unsub_links = async_track_state_change_event(
                self.hass, targets, self._handle_linked_change
            )

    @callback
    def _stop_following_links(self) -> None:
        if self._unsub_links is not None:
            self._unsub_links()
            self._unsub_links = None
        self._link_targets = frozenset()

    @callback
    def _handle_linked_change(self, event: Event[EventStateChangedData]) -> None:
        """Show a linked media player's new track or state."""
        self.async_write_ha_state()

    async def _async_forward(self, service: str, **data: Any) -> bool:
        """Pass a playback command to the media player standing in, if any."""
        if (linked := self._linked_state()) is None:
            return False
        await self.hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            service,
            {ATTR_ENTITY_ID: linked.entity_id, **data},
            blocking=True,
        )
        return True

    @property
    def group_members(self) -> list[str]:
        """Return the players grouped with this one, master first."""
        return self._resolve_group_members()

    async def async_added_to_hass(self) -> None:
        """Start following other players, and tell them about this one."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, _SIGNAL_PLAYERS_CHANGED, self._handle_players_changed
            )
        )
        async_dispatcher_send(self.hass, _SIGNAL_PLAYERS_CHANGED, self)
        self._follow_links()

    async def async_will_remove_from_hass(self) -> None:
        """Tell the other players this one is going."""
        await super().async_will_remove_from_hass()
        self._stop_following_links()
        async_dispatcher_send(self.hass, _SIGNAL_PLAYERS_CHANGED, self)

    @callback
    def _handle_players_changed(self, sender: BluesoundMediaPlayer) -> None:
        """Re-resolve group members after another player came or went."""
        if sender is not self:
            self._follow_links()
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic_volume = None
        self._optimistic_muted = None
        self._attr_media_position_updated_at = dt_util.utcnow()
        self._follow_links()
        super()._handle_coordinator_update()

    def _resolve_group_members(self) -> list[str]:
        """Return entity IDs for all players in this group (master + slaves)."""
        coord = self.coordinator
        if not coord.group_slaves and not coord.group_master_ip:
            return []

        ent_reg = er.async_get(self.hass)
        all_coordinators = self._all_coordinators()

        def _entity_id_for_ip(ip: str) -> str | None:
            for c in all_coordinators.values():
                if c.host == ip:
                    return ent_reg.async_get_entity_id(
                        "media_player", DOMAIN, c.sync_info.mac
                    )
            return None

        if coord.group_slaves:
            # This player is master, self + all slaves
            members = [self.entity_id]
            for slave in coord.group_slaves:
                eid = _entity_id_for_ip(slave["ip"])
                if eid:
                    members.append(eid)
            return members

        if coord.group_master_ip:
            # This player is a slave, find master and return full group
            master_coord = self._find_coordinator_by_ip(coord.group_master_ip)
            if master_coord and master_coord.group_slaves:
                master_eid = _entity_id_for_ip(coord.group_master_ip)
                members = [master_eid] if master_eid else []
                for slave in master_coord.group_slaves:
                    eid = _entity_id_for_ip(slave["ip"])
                    if eid:
                        members.append(eid)
                return members

        return []

    # --- Playback controls ---

    async def async_media_play(self) -> None:
        """Start or resume playback."""
        if await self._async_forward("media_play"):
            return
        await self.coordinator.async_request_api("/Play")
        await self.coordinator.async_request_refresh()

    async def async_media_pause(self) -> None:
        """Pause playback.

        Plain /Pause, not toggle=1: a toggle resumes a player that was
        already paused, e.g. from the BluOS app before our state caught up.
        """
        if await self._async_forward("media_pause"):
            return
        await self.coordinator.async_request_api("/Pause")
        await self.coordinator.async_request_refresh()

    async def async_media_stop(self) -> None:
        """Stop playback."""
        if await self._async_forward("media_stop"):
            return
        await self.coordinator.async_request_api("/Stop")
        await self.coordinator.async_request_refresh()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set this player's volume, showing the new level right away."""
        self._optimistic_volume = volume
        self.async_write_ha_state()
        await self._send_volume(level=round(volume * 100))

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute this player, showing the change right away."""
        self._optimistic_muted = mute
        self.async_write_ha_state()
        await self._send_volume(mute=int(mute))

    async def _send_volume(self, **params: int) -> None:
        """Send a /Volume change and show what the player says it now is."""
        try:
            answer = await self.coordinator.async_request_api("/Volume", **params)
        except HomeAssistantError:
            self._optimistic_volume = None
            self._optimistic_muted = None
            self.async_write_ha_state()
            raise
        self.coordinator.async_apply_volume_answer(answer)

    async def async_select_source(self, source: str) -> None:
        """Switch to an input by its name.

        Services such as Spotify and presets are no longer listed as sources,
        but their names are still accepted, so automations that picked one
        this way keep working.
        """
        for s in self.coordinator.sources:
            if s["name"] == source:
                await self.coordinator.async_request_api("/Play", url=s["play_url"])
                await self.coordinator.async_request_refresh()
                return
        for i in self.coordinator.external_inputs:
            if i["name"] == source:
                # Not in the public API; found on a NAD T758, whose BluOS app
                # switches its inputs this way.
                await self.coordinator.async_request_api("/ExternalSource", id=i["id"])
                await self.coordinator.async_request_refresh()
                return
        for p in self.coordinator.presets:
            if p["name"] == source:
                await self.coordinator.async_request_api("/Preset", id=p["id"])
                await self.coordinator.async_request_refresh()
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_source",
            translation_placeholders={"source": source},
        )

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Turn shuffle on or off."""
        await self.coordinator.async_request_api("/Shuffle", state=int(shuffle))
        await self.coordinator.async_request_refresh()

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set the repeat mode."""
        await self.coordinator.async_request_api(
            "/Repeat", state=_HA_TO_REPEAT[repeat]
        )
        await self.coordinator.async_request_refresh()

    async def async_media_next_track(self) -> None:
        """Skip to the next track."""
        if await self._async_forward("media_next_track"):
            return
        await self.coordinator.async_request_api("/Skip")
        await self.coordinator.async_request_refresh()

    async def async_media_previous_track(self) -> None:
        """Go back to the previous track."""
        if await self._async_forward("media_previous_track"):
            return
        await self.coordinator.async_request_api("/Back")
        await self.coordinator.async_request_refresh()

    async def async_media_seek(self, position: float) -> None:
        """Jump to a position in the track."""
        seek = {ATTR_MEDIA_SEEK_POSITION: position}
        if await self._async_forward("media_seek", **seek):
            return
        await self.coordinator.async_request_api("/Play", seek=int(position))
        await self.coordinator.async_request_refresh()

    async def async_clear_playlist(self) -> None:
        """Empty the play queue."""
        await self.coordinator.async_request_api("/Clear")
        await self.coordinator.async_request_refresh()

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a URL on the device, resolving HA media_source IDs first."""
        if media_id.startswith(_BLUESOUND_PRESET):
            await self._async_play_preset(media_id.removeprefix(_BLUESOUND_PRESET))
            return
        if media_id.startswith(_BLUESOUND_LINKED):
            await self._async_play_linked(media_id)
            return
        # Items picked from our own media browser carry a BluOS playURL, which is
        # replayed verbatim (it is already a complete, encoded relative URL).
        if media_id.startswith(_BLUESOUND_SCHEME):
            _, play_url = _decode_media_id(media_id)
            if play_url:
                await self.coordinator.async_play_path(play_url)
                await self.coordinator.async_request_refresh()
            return

        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = play_item.url

        media_id = async_process_play_media_url(self.hass, media_id)

        await self.coordinator.async_request_api("/Play", url=media_id)
        await self.coordinator.async_request_refresh()

    async def _async_play_linked(self, media_id: str) -> None:
        """Switch to a linked input, and play an item of its media player there.

        The input alone just switches; an item from the linked player's media
        list is then played by that player, into the input.
        """
        key, content_type, content_id = _decode_linked(media_id)
        linked = self._browsable_link(key) if content_id is not None else None
        if content_id is not None and linked is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="not_linked"
            )
        if key.startswith("browse:"):
            await self.coordinator.async_request_api(
                "/Play", url=key.removeprefix("browse:")
            )
        elif key.startswith("input:"):
            await self.coordinator.async_request_api(
                "/ExternalSource", id=key.removeprefix("input:")
            )
        if linked is not None:
            await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                "play_media",
                {
                    ATTR_ENTITY_ID: linked.entity_id,
                    ATTR_MEDIA_CONTENT_TYPE: content_type,
                    ATTR_MEDIA_CONTENT_ID: content_id,
                },
                blocking=True,
            )
        await self.coordinator.async_request_refresh()

    async def _async_play_preset(self, preset_id: str) -> None:
        """Load one of the player's presets by its id."""
        if not any(p["id"] == preset_id for p in self.coordinator.presets):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_preset",
                translation_placeholders={"preset": preset_id},
            )
        await self.coordinator.async_request_api("/Preset", id=preset_id)
        await self.coordinator.async_request_refresh()

    # --- Media browsing ---

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the device's services/radio tree and HA media sources."""
        if media_content_id is None or media_content_id == _BLUESOUND_ROOT:
            return await self._build_browse_root()
        if media_content_id == _BLUESOUND_PRESETS:
            return self._build_presets_folder()
        if media_content_id.startswith(_BLUESOUND_LINKED):
            return await self._browse_linked(media_content_id)

        if media_source.is_media_source_id(media_content_id):
            return await media_source.async_browse_media(
                self.hass,
                media_content_id,
                content_filter=lambda item: item.media_content_type.startswith(
                    "audio/"
                ),
            )

        browse_key, _ = _decode_media_id(media_content_id)
        items = await self.coordinator.async_browse(browse_key or None)
        return BrowseMedia(
            title="Bluesound",
            media_class=MediaClass.DIRECTORY,
            media_content_type="bluesound",
            media_content_id=media_content_id,
            can_play=False,
            can_expand=True,
            children=[self._item_to_browse(i) for i in items],
            children_media_class=MediaClass.DIRECTORY,
        )

    async def _build_browse_root(self) -> BrowseMedia:
        """Top level: device services/inputs/radio plus HA media sources."""
        items = await self.coordinator.async_browse(None)
        children = [self._linked_item(i) or self._item_to_browse(i) for i in items]
        if self.coordinator.presets:
            children.insert(0, self._build_presets_folder(with_children=False))
        with contextlib.suppress(BrowseError):
            children.append(
                await media_source.async_browse_media(
                    self.hass,
                    None,
                    content_filter=lambda item: item.media_content_type.startswith(
                        "audio/"
                    ),
                )
            )
        return BrowseMedia(
            title="Bluesound",
            media_class=MediaClass.DIRECTORY,
            media_content_type="bluesound",
            media_content_id=_BLUESOUND_ROOT,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _build_presets_folder(self, with_children: bool = True) -> BrowseMedia:
        """Return the player's presets as a folder, each played with one tap."""
        children = None
        if with_children:
            children = [
                BrowseMedia(
                    title=p["name"],
                    media_class=MediaClass.MUSIC,
                    media_content_type=MediaType.MUSIC,
                    media_content_id=f"{_BLUESOUND_PRESET}{p['id']}",
                    can_play=True,
                    can_expand=False,
                    thumbnail=self._browse_thumbnail(
                        f"{_BLUESOUND_PRESET}{p['id']}", p.get("image")
                    ),
                )
                for p in self.coordinator.presets
            ]
        return BrowseMedia(
            title="Presets",
            media_class=MediaClass.DIRECTORY,
            media_content_type="bluesound",
            media_content_id=_BLUESOUND_PRESETS,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.MUSIC,
        )

    def _linked_item(self, item: dict[str, str | None]) -> BrowseMedia | None:
        """Return a linked input as a folder of its media player's media.

        It still plays, which switches to the input. None for an input that
        is not linked to a media player that can be browsed.
        """
        play_url = item.get("play_url")
        if not play_url or "?url=" not in play_url:
            return None
        key = f"browse:{unquote(play_url.split('?url=', 1)[1])}"
        if self._browsable_link(key) is None:
            return None
        media_content_id = _encode_linked(key)
        return BrowseMedia(
            title=item["name"],
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            media_content_id=media_content_id,
            can_play=True,
            can_expand=True,
            thumbnail=self._browse_thumbnail(media_content_id, item.get("image")),
        )

    async def _browse_linked(self, media_content_id: str) -> BrowseMedia:
        """Browse a linked input's media player, through this player."""
        key, content_type, content_id = _decode_linked(media_content_id)
        if (linked := self._browsable_link(key)) is None:
            raise BrowseError(f"{key} is not linked to a media player to browse")
        media = await linked.async_browse_media(content_type, content_id)
        return self._wrap_linked(key, media)

    def _wrap_linked(self, key: str, media: BrowseMedia) -> BrowseMedia:
        """Return a linked player's media, its ids leading back through us.

        So opening or playing an item asks this player, which switches to the
        input and passes the item on. Searching is not passed on.
        """
        wrapped = copy.copy(media)
        wrapped.media_content_id = _encode_linked(
            key, media.media_content_type, media.media_content_id
        )
        wrapped.can_search = False
        if media.children:
            wrapped.children = [self._wrap_linked(key, c) for c in media.children]
        return wrapped

    def _item_to_browse(self, item: dict[str, str | None]) -> BrowseMedia:
        browse_key = item.get("browse_key")
        play_url = item.get("play_url")
        media_content_id = _encode_media_id(browse_key, play_url)
        return BrowseMedia(
            title=item["name"],
            media_class=MediaClass.MUSIC if play_url else MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            media_content_id=media_content_id,
            can_play=play_url is not None,
            can_expand=browse_key is not None,
            thumbnail=self._browse_thumbnail(media_content_id, item.get("image")),
        )

    def _abs_image(self, image: str | None) -> str | None:
        if not image:
            return None
        if image.startswith("http"):
            return image
        return f"http://{self.coordinator.host}:{self.coordinator.port}{image}"

    def _browse_thumbnail(
        self, media_content_id: str, image: str | None
    ) -> str | None:
        """Return where the frontend loads an item's image from.

        HTTPS images load as they are; others come through Home Assistant.
        """
        if not image:
            return None
        if image.startswith("https://"):
            return image
        self._browse_images[image] = None
        self._browse_images.move_to_end(image)
        while len(self._browse_images) > _MAX_BROWSE_IMAGES:
            self._browse_images.popitem(last=False)
        return self.get_browse_image_url(
            MediaType.MUSIC, media_content_id, media_image_id=image
        )

    async def async_get_browse_image(
        self,
        media_content_type: str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """Fetch a browse image for the frontend, if it was handed out."""
        if media_image_id is None or media_image_id not in self._browse_images:
            return None, None
        return await self.coordinator.async_fetch_image(self._abs_image(media_image_id))

    # --- Grouping ---

    async def async_join_players(self, group_members: list[str]) -> None:
        """Group the selected players with this one.

        Home Assistant's group dialog sends the whole selection, including
        players already in the group; see grouping.py for how each is handled.
        """
        ent_reg = er.async_get(self.hass)
        all_coordinators = self._all_coordinators()
        members = []
        for member_entity_id in group_members:
            if member_entity_id == self.entity_id:
                continue
            entry = ent_reg.async_get(member_entity_id)
            coord = all_coordinators.get(entry.config_entry_id) if entry else None
            if coord is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_member",
                    translation_placeholders={"entity_id": member_entity_id},
                )
            members.append((coord.host, coord.port))

        coord = self.coordinator
        touched = await async_join(
            coord._get_session(), (coord.host, coord.port), members
        )
        await self._refresh_players(touched)

    async def async_unjoin_player(self) -> None:
        """Take this player out of its group.

        A leader hands its group to the others where its playback can move to
        them, and lets them all go where it cannot.
        """
        coord = self.coordinator
        touched = await async_unjoin(coord._get_session(), (coord.host, coord.port))
        await self._refresh_players(touched)

    def _all_coordinators(self) -> dict[str, BluesoundCoordinator]:
        """Coordinators of every set-up player, by config entry id.

        Includes players still finishing setup: their entities are added (and
        announce themselves to the others) before the entry counts as loaded.
        """
        return {
            entry.entry_id: entry.runtime_data
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.state
            in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
            and isinstance(getattr(entry, "runtime_data", None), BluesoundCoordinator)
        }

    def _find_coordinator_by_ip(self, ip: str) -> BluesoundCoordinator | None:
        for coord in self._all_coordinators().values():
            if coord.host == ip:
                return coord
        return None

    async def _refresh_players(self, addresses: set[tuple[str, int]]) -> None:
        """Refresh every set-up player at one of these addresses."""
        for coord in self._all_coordinators().values():
            if (coord.host, coord.port) in addresses:
                await coord.async_refresh()
