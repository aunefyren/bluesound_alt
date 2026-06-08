"""Bluesound Alt media player entity."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from homeassistant.components import media_source
from homeassistant.components.media_player import (
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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import BluesoundCoordinator

_LOGGER = logging.getLogger(__name__)

SUPPORT_BLUESOUND = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.BROWSE_MEDIA
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

# Media browser: media_content_id encodes a BluOS browseKey (b) and/or playURL (p).
_BLUESOUND_SCHEME = "bluesound:"
_BLUESOUND_ROOT = "bluesound:root"


def _encode_media_id(browse_key: str | None, play_url: str | None) -> str:
    """Encode a BluOS browseKey/playURL pair into a media_content_id."""
    parts: list[tuple[str, str]] = []
    if browse_key is not None:
        parts.append(("b", browse_key))
    if play_url is not None:
        parts.append(("p", play_url))
    return f"{_BLUESOUND_SCHEME}?{urlencode(parts)}"


def _decode_media_id(content_id: str) -> tuple[str | None, str | None]:
    """Return (browse_key, play_url) decoded from a media_content_id."""
    qs = parse_qs(urlsplit(content_id).query, keep_blank_values=True)
    browse_key = qs["b"][0] if "b" in qs else None
    play_url = qs["p"][0] if "p" in qs else None
    return browse_key, play_url


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BluesoundCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BluesoundMediaPlayer(coordinator, entry)])


class BluesoundMediaPlayer(CoordinatorEntity[BluesoundCoordinator], MediaPlayerEntity):
    """Representation of a Bluesound player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = SUPPORT_BLUESOUND
    _attr_media_position_updated_at = None

    def __init__(self, coordinator: BluesoundCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = coordinator.sync_info.mac
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.sync_info.mac)},
            "name": coordinator.sync_info.name,
            "model": coordinator.sync_info.model,
            "manufacturer": "Bluesound",
        }
        self._optimistic_volume: float | None = None
        self._optimistic_muted: bool | None = None

    @property
    def state(self) -> MediaPlayerState | None:
        return _STATE_MAP.get(self.coordinator.data.state)

    @property
    def volume_level(self) -> float | None:
        if self._optimistic_volume is not None:
            return self._optimistic_volume
        if self.coordinator.group_master_ip and self.coordinator.individual_volume is not None:
            return self.coordinator.individual_volume / 100
        return self.coordinator.data.volume / 100

    @property
    def is_volume_muted(self) -> bool:
        if self._optimistic_muted is not None:
            return self._optimistic_muted
        return self.coordinator.data.muted

    @property
    def shuffle(self) -> bool:
        return self.coordinator.data.shuffle

    @property
    def repeat(self) -> RepeatMode | None:
        return _REPEAT_TO_HA.get(self.coordinator.data.repeat)

    @property
    def media_title(self) -> str | None:
        return self.coordinator.data.title

    @property
    def media_artist(self) -> str | None:
        return self.coordinator.data.artist

    @property
    def media_album_name(self) -> str | None:
        return self.coordinator.data.album

    @property
    def media_image_url(self) -> str | None:
        img = self.coordinator.data.image_url
        if img and not img.startswith("http"):
            return f"http://{self.coordinator.host}:{self.coordinator.port}{img}"
        return img

    @property
    def media_position(self) -> float | None:
        return self.coordinator.data.position

    @property
    def media_duration(self) -> float | None:
        return self.coordinator.data.duration

    @property
    def source_list(self) -> list[str]:
        return [s["name"] for s in self.coordinator.sources] + [
            p["name"] for p in self.coordinator.presets
        ]

    @property
    def source(self) -> str | None:
        stream_url = self.coordinator.data.stream_url
        if stream_url:
            for s in self.coordinator.sources:
                if s["play_url"] == stream_url:
                    return s["name"]
        # Push inputs (AirPlay, URL/streaming) match no /Browse source, fall
        # back to the active BluOS service so the UI shows what is playing.
        data = self.coordinator.data
        if not data.service:
            return None
        return _SERVICE_LABELS.get(data.service, data.service_name or data.service)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"bluesound_group": self.group_members}

    @property
    def group_members(self) -> list[str]:
        return self._resolve_group_members()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic_volume = None
        self._optimistic_muted = None
        self._attr_media_position_updated_at = dt_util.utcnow()
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
        await self.coordinator.async_request_api("/Play")
        await self.coordinator.async_request_refresh()

    async def async_media_pause(self) -> None:
        await self.coordinator.async_request_api("/Pause", toggle=1)
        await self.coordinator.async_request_refresh()

    async def async_media_stop(self) -> None:
        await self.coordinator.async_request_api("/Stop")
        await self.coordinator.async_request_refresh()

    async def async_set_volume_level(self, volume: float) -> None:
        self._optimistic_volume = volume
        self.async_write_ha_state()
        await self.coordinator.async_request_api("/Volume", level=int(volume * 100))
        await self.coordinator.async_refresh_individual_volume()

    async def async_mute_volume(self, mute: bool) -> None:
        self._optimistic_muted = mute
        self.async_write_ha_state()
        await self.coordinator.async_request_api("/Volume", mute=int(mute))
        await self.coordinator.async_request_refresh()

    async def async_select_source(self, source: str) -> None:
        for s in self.coordinator.sources:
            if s["name"] == source:
                await self.coordinator.async_request_api("/Play", url=s["play_url"])
                await self.coordinator.async_request_refresh()
                return
        for p in self.coordinator.presets:
            if p["name"] == source:
                await self.coordinator.async_request_api("/Preset", id=p["id"])
                await self.coordinator.async_request_refresh()
                return

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self.coordinator.async_request_api("/Shuffle", state=int(shuffle))
        await self.coordinator.async_request_refresh()

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        await self.coordinator.async_request_api(
            "/Repeat", state=_HA_TO_REPEAT[repeat]
        )
        await self.coordinator.async_request_refresh()

    async def async_media_next_track(self) -> None:
        await self.coordinator.async_request_api("/Skip")
        await self.coordinator.async_request_refresh()

    async def async_media_previous_track(self) -> None:
        await self.coordinator.async_request_api("/Back")
        await self.coordinator.async_request_refresh()

    async def async_media_seek(self, position: float) -> None:
        await self.coordinator.async_request_api("/Play", seek=int(position))
        await self.coordinator.async_request_refresh()

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a URL on the device, resolving HA media_source IDs first."""
        # Items picked from our own media browser carry a BluOS playURL.
        if media_id.startswith(_BLUESOUND_SCHEME):
            _, play_url = _decode_media_id(media_id)
            if play_url:
                await self.coordinator.async_request_api("/Play", url=play_url)
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

    # --- Media browsing ---

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the device's services/radio tree and HA media sources."""
        if media_content_id is None or media_content_id == _BLUESOUND_ROOT:
            return await self._build_browse_root()

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
        children = [self._item_to_browse(i) for i in items]
        try:
            children.append(
                await media_source.async_browse_media(
                    self.hass,
                    None,
                    content_filter=lambda item: item.media_content_type.startswith(
                        "audio/"
                    ),
                )
            )
        except BrowseError:
            pass
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

    def _item_to_browse(self, item: dict[str, str | None]) -> BrowseMedia:
        browse_key = item.get("browse_key")
        play_url = item.get("play_url")
        return BrowseMedia(
            title=item["name"],
            media_class=MediaClass.MUSIC if play_url else MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            media_content_id=_encode_media_id(browse_key, play_url),
            can_play=play_url is not None,
            can_expand=browse_key is not None,
            thumbnail=self._abs_image(item.get("image")),
        )

    def _abs_image(self, image: str | None) -> str | None:
        if not image:
            return None
        if image.startswith("http"):
            return image
        return f"http://{self.coordinator.host}:{self.coordinator.port}{image}"

    # --- Grouping ---

    async def async_join_players(self, group_members: list[str]) -> None:
        """Make this player the master and join group_members as slaves."""
        all_coordinators = self._all_coordinators()
        ent_reg = er.async_get(self.hass)

        for member_entity_id in group_members:
            if member_entity_id == self.entity_id:
                continue
            entry = ent_reg.async_get(member_entity_id)
            if not entry:
                continue
            coord = all_coordinators.get(entry.config_entry_id)
            if not coord:
                continue
            await self.coordinator.async_request_api(
                "/AddSlave",
                slave=coord.host,
                port=coord.port,
            )

        await self._refresh_all(group_members)

    async def async_unjoin_player(self) -> None:
        """Remove this player from its group."""
        coord = self.coordinator

        if coord.group_master_ip:
            # This player is a slave, ask master to remove us
            master_coord = self._find_coordinator_by_ip(coord.group_master_ip)
            if master_coord:
                await master_coord.async_request_api(
                    "/RemoveSlave",
                    slave=coord.host,
                    port=coord.port,
                )
                await master_coord.async_refresh()
            else:
                await coord.async_request_api(
                    "/RemoveSlave",
                    slave=coord.host,
                    port=coord.port,
                )
        else:
            # This player is master, unjoin all slaves
            for slave in coord.group_slaves:
                await coord.async_request_api(
                    "/RemoveSlave",
                    slave=slave["ip"],
                    port=slave["port"],
                )
                slave_coord = self._find_coordinator_by_ip(slave["ip"])
                if slave_coord:
                    await slave_coord.async_refresh()

        await coord.async_refresh()

    def _all_coordinators(self) -> dict[str, BluesoundCoordinator]:
        return {
            entry_id: coord
            for entry_id, coord in self.hass.data.get(DOMAIN, {}).items()
            if isinstance(coord, BluesoundCoordinator)
        }

    def _find_coordinator_by_ip(self, ip: str) -> BluesoundCoordinator | None:
        for coord in self._all_coordinators().values():
            if coord.host == ip:
                return coord
        return None

    async def _refresh_all(self, entity_ids: list[str]) -> None:
        ent_reg = er.async_get(self.hass)
        all_coords = self._all_coordinators()
        await self.coordinator.async_refresh()
        for eid in entity_ids:
            if eid == self.entity_id:
                continue
            entry = ent_reg.async_get(eid)
            if not entry:
                continue
            coord = all_coords.get(entry.config_entry_id)
            if coord:
                await coord.async_refresh()
