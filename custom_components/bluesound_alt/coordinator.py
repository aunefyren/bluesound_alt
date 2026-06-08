"""DataUpdateCoordinator for Bluesound Alt."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

import aiohttp
import xmltodict
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LONG_POLL_TIMEOUT, NODE_OFFLINE_CHECK_TIMEOUT

_LOGGER = logging.getLogger(__name__)


@dataclass
class BluesoundData:
    """All state parsed from /Status."""

    state: str = "stop"
    volume: int = 0
    muted: bool = False
    shuffle: bool = False
    repeat: int = 0
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    image_url: str | None = None
    position: float | None = None
    duration: float | None = None
    service: str | None = None
    service_name: str | None = None
    stream_url: str | None = None
    group_name: str | None = None
    sync_stat: str | None = None
    etag: str | None = None


@dataclass
class BluesoundSyncInfo:
    """Device identity and group topology from /SyncStatus."""

    name: str
    mac: str
    model: str | None = None
    ip: str | None = None
    port: int = 11000
    # Group topology, master_ip set means this player is a slave
    master_ip: str | None = None
    slaves: list[dict[str, Any]] = field(default_factory=list)


class BluesoundCoordinator(DataUpdateCoordinator[BluesoundData]):
    """Coordinator for a single Bluesound player."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        sync_info: BluesoundSyncInfo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=None,
        )
        self.host = host
        self.port = port
        self.sync_info = sync_info
        # Group topology, kept current by re-fetching /SyncStatus when syncStat changes
        self.group_master_ip: str | None = sync_info.master_ip
        self.group_slaves: list[dict[str, Any]] = sync_info.slaves
        self._last_sync_stat: str | None = None
        # Sources fetched once from /Browse: [{name, play_url}]
        self.sources: list[dict[str, str]] = []
        # Presets (saved radio/favourites) fetched once from /Presets: [{id, name, image}]
        self.presets: list[dict[str, str]] = []
        # Individual volume for this device (differs from group volume when slave)
        self.individual_volume: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._long_poll_task: asyncio.Task | None = None
        self._volume_poll_task: asyncio.Task | None = None
        self._is_first_fetch = True

    def _base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def _fetch_status(self, etag: str | None = None) -> BluesoundData:
        session = self._get_session()
        url = f"{self._base_url()}/Status"

        if etag:
            params = {"timeout": LONG_POLL_TIMEOUT, "etag": etag}
            request_timeout = LONG_POLL_TIMEOUT + NODE_OFFLINE_CHECK_TIMEOUT
        else:
            params = {}
            request_timeout = NODE_OFFLINE_CHECK_TIMEOUT

        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=request_timeout),
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Bad status {resp.status} from {url}")
            text = await resp.text()

        return _parse_status(text)

    async def _fetch_volume(self) -> int | None:
        """Fetch /Volume and return the device's individual volume level."""
        session = self._get_session()
        url = f"{self._base_url()}/Volume"
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT)
            ) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
            parsed = xmltodict.parse(text)
            vol = parsed.get("volume")
            if isinstance(vol, dict):
                return _safe_int(vol.get("#text"))
            return _safe_int(vol)
        except Exception:
            return None

    async def async_refresh_individual_volume(self) -> None:
        """Fetch /Volume, store individual volume, notify listeners."""
        vol = await self._fetch_volume()
        if vol is not None and self.data is not None:
            self.individual_volume = vol
            self.async_set_updated_data(self.data)

    async def _refresh_group_topology(self) -> None:
        """Re-fetch /SyncStatus to update group master/slave info."""
        session = self._get_session()
        info = await _fetch_sync_info(session, self.host, self.port)
        if info:
            self.group_master_ip = info.master_ip
            self.group_slaves = info.slaves
            _LOGGER.debug(
                "%s group topology: master=%s slaves=%s",
                self.host,
                self.group_master_ip,
                [s["ip"] for s in self.group_slaves],
            )
            await self.async_refresh_individual_volume()
            if self.group_master_ip:
                self._start_volume_poll_loop()
            else:
                self._stop_volume_poll_loop()

    def _start_volume_poll_loop(self) -> None:
        if self._volume_poll_task and not self._volume_poll_task.done():
            return
        self._volume_poll_task = self.hass.async_create_task(
            self._volume_poll_loop(), name=f"bluesound_alt_volume_{self.host}"
        )

    def _stop_volume_poll_loop(self) -> None:
        if self._volume_poll_task and not self._volume_poll_task.done():
            self._volume_poll_task.cancel()
            self._volume_poll_task = None

    async def _volume_poll_loop(self) -> None:
        """Long-poll /Volume to track individual slave volume changes from any source."""
        session = self._get_session()
        url = f"{self._base_url()}/Volume"
        etag: str | None = None

        while True:
            try:
                if etag:
                    params = {"timeout": LONG_POLL_TIMEOUT, "etag": etag}
                    request_timeout = LONG_POLL_TIMEOUT + NODE_OFFLINE_CHECK_TIMEOUT
                else:
                    params = {}
                    request_timeout = NODE_OFFLINE_CHECK_TIMEOUT

                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=request_timeout),
                ) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(NODE_OFFLINE_CHECK_TIMEOUT)
                        continue
                    text = await resp.text()

                parsed = xmltodict.parse(text)
                vol_data = parsed.get("volume", {})
                if isinstance(vol_data, dict):
                    new_etag = vol_data.get("@etag")
                    vol = _safe_int(vol_data.get("#text"))
                else:
                    new_etag = None
                    vol = _safe_int(vol_data)

                etag = new_etag
                if self.data is not None:
                    self.individual_volume = vol
                    self.async_set_updated_data(self.data)

            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Volume poll error for %s: %s", self.host, err)
                etag = None
                await asyncio.sleep(NODE_OFFLINE_CHECK_TIMEOUT)
            except asyncio.CancelledError:
                return

    async def _async_update_data(self) -> BluesoundData:
        try:
            data = await self._fetch_status(etag=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Cannot connect to {self.host}: {err}") from err

        await self._maybe_refresh_topology(data)

        if self._is_first_fetch:
            self._is_first_fetch = False
            self.sources = await _fetch_sources(self._get_session(), self.host, self.port)
            self.presets = await _fetch_presets(self._get_session(), self.host, self.port)
            self._start_long_poll_loop()

        return data

    async def _maybe_refresh_topology(self, data: BluesoundData) -> None:
        if data.sync_stat != self._last_sync_stat:
            self._last_sync_stat = data.sync_stat
            await self._refresh_group_topology()

    def _start_long_poll_loop(self) -> None:
        if self._long_poll_task and not self._long_poll_task.done():
            return
        self._long_poll_task = self.hass.async_create_task(
            self._long_poll_loop(), name=f"bluesound_alt_poll_{self.host}"
        )

    async def _long_poll_loop(self) -> None:
        etag: str | None = self.data.etag if self.data else None

        while True:
            try:
                data = await self._fetch_status(etag=etag)
                await self._maybe_refresh_topology(data)
                etag = data.etag
                self.async_set_updated_data(data)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Long-poll error for %s: %s, retrying", self.host, err)
                self.async_set_update_error(UpdateFailed(str(err)))
                etag = None
                await asyncio.sleep(NODE_OFFLINE_CHECK_TIMEOUT)
            except asyncio.CancelledError:
                return

    async def async_request_api(self, path: str, **params: Any) -> None:
        session = self._get_session()
        url = f"{self._base_url()}{path}"
        try:
            async with session.get(
                url,
                params=params or None,
                timeout=aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Command %s returned %s", url, resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Command %s failed: %s", url, err)

    async def async_browse(self, key: str | None = None) -> list[dict[str, str | None]]:
        """Fetch /Browse (optionally for a browseKey) and return parsed items."""
        session = self._get_session()
        url = f"{self._base_url()}/Browse"
        params = {"key": key} if key else None
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Browse %s returned %s", key, resp.status)
                    return []
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Browse %s failed: %s", key, err)
            return []
        return _parse_browse_items(text)

    async def async_play_path(self, path: str) -> None:
        """GET a device-provided relative URL (e.g. a browse playURL) verbatim.

        The path is already percent-encoded by the device, so it is sent
        unchanged (encoded=True) rather than decoded and rebuilt.
        """
        session = self._get_session()
        url = URL(f"{self._base_url()}{path}", encoded=True)
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Play %s returned %s", path, resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Play %s failed: %s", path, err)

    def stop(self) -> None:
        if self._long_poll_task and not self._long_poll_task.done():
            self._long_poll_task.cancel()
        self._stop_volume_poll_loop()


async def _fetch_sync_info(
    session: aiohttp.ClientSession, host: str, port: int
) -> BluesoundSyncInfo | None:
    """Fetch /SyncStatus and return device identity + group topology."""
    url = f"http://{host}:{port}/SyncStatus"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    try:
        parsed = xmltodict.parse(text)
        sync = parsed.get("SyncStatus", {})
        mac = sync.get("@mac", "").replace(":", "").lower()
        name = sync.get("@name") or f"Bluesound {host}"
        model = sync.get("@modelName") or sync.get("@model")

        # Slaves, attribute is @id (IP), not @ip
        slaves_raw = sync.get("slave", [])
        if isinstance(slaves_raw, dict):
            slaves_raw = [slaves_raw]
        slaves = [
            {
                "ip": sl.get("@id", ""),
                "port": _safe_int(sl.get("@port"), 11000),
                "name": sl.get("@name", ""),
            }
            for sl in slaves_raw
            if isinstance(sl, dict) and sl.get("@id")
        ]

        # Master, text content of <master> element, not an attribute
        master_ip: str | None = None
        master_raw = sync.get("master")
        if master_raw:
            if isinstance(master_raw, dict):
                master_ip = master_raw.get("#text") or None
            elif isinstance(master_raw, str) and master_raw.strip():
                master_ip = master_raw.strip()

        return BluesoundSyncInfo(
            name=name,
            mac=mac,
            model=model,
            ip=host,
            port=port,
            master_ip=master_ip,
            slaves=slaves,
        )
    except Exception:
        _LOGGER.exception("Failed to parse SyncStatus from %s", host)
        return None


async def _fetch_sources(
    session: aiohttp.ClientSession, host: str, port: int
) -> list[dict[str, str]]:
    """Fetch /Browse and return directly-playable sources (type=audio only)."""
    url = f"http://{host}:{port}/Browse"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []

    try:
        parsed = xmltodict.parse(text)
        items = parsed.get("browse", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]

        sources = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "audio":
                continue
            play_url_raw = item.get("@playURL", "")
            # playURL is like /Play?url=Capture%3A..., extract the url param
            if "?url=" not in play_url_raw:
                continue
            encoded = play_url_raw.split("?url=", 1)[1]
            from urllib.parse import unquote
            play_url = unquote(encoded)
            sources.append({"name": item.get("@text", ""), "play_url": play_url})

        return sources
    except Exception:
        _LOGGER.exception("Failed to parse Browse from %s", host)
        return []


async def _fetch_presets(
    session: aiohttp.ClientSession, host: str, port: int
) -> list[dict[str, str]]:
    """Fetch /Presets and return saved radio/favourites: [{id, name, image}]."""
    url = f"http://{host}:{port}/Presets"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []

    try:
        parsed = xmltodict.parse(text)
        items = parsed.get("presets", {}).get("preset", [])
        if isinstance(items, dict):
            items = [items]

        presets = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = item.get("@id")
            name = item.get("@name")
            if pid is None or not name:
                continue
            presets.append({"id": pid, "name": name, "image": item.get("@image", "")})

        return presets
    except Exception:
        _LOGGER.exception("Failed to parse Presets from %s", host)
        return []


def _parse_browse_items(xml_text: str) -> list[dict[str, str | None]]:
    """Parse a /Browse response into [{name, image, browse_key, play_url}].

    play_url is the device-provided relative URL (e.g. /Play?url=...) kept
    verbatim so it can be replayed exactly via async_play_path().
    """
    parsed = xmltodict.parse(xml_text)
    root = parsed.get("browse") or parsed.get("radiotime") or {}
    items = root.get("item", [])
    if isinstance(items, dict):
        items = [items]

    result: list[dict[str, str | None]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("@text") or item.get("@title")
        if not name:
            continue

        # playURL marks a playable item; legacy radiotime audio uses @URL, which
        # needs wrapping in /Play?url=.
        play_url: str | None = item.get("@playURL")
        if not play_url and item.get("@type") == "audio" and item.get("@URL"):
            play_url = f"/Play?url={item['@URL']}"

        # browseKey marks a navigable node; @key is the legacy radiotime fallback.
        browse_key = item.get("@browseKey")
        if not browse_key and item.get("@type") == "link":
            browse_key = item.get("@key")

        result.append(
            {
                "name": name,
                "image": item.get("@image"),
                "browse_key": browse_key,
                "play_url": play_url,
            }
        )
    return result


def _parse_status(xml_text: str) -> BluesoundData:
    parsed = xmltodict.parse(xml_text)
    s = parsed.get("status", {})

    data = BluesoundData()
    data.etag = s.get("@etag")
    data.state = s.get("state", "stop")
    data.muted = s.get("mute", "0") == "1"
    data.shuffle = s.get("shuffle", "0") == "1"
    data.service = s.get("service")
    data.service_name = s.get("serviceName")
    data.title = s.get("title1")
    data.artist = s.get("title2")
    data.album = s.get("title3")
    data.image_url = s.get("image")
    data.group_name = s.get("groupName")
    data.sync_stat = s.get("syncStat")
    data.stream_url = s.get("streamUrl")

    try:
        data.volume = int(s.get("volume", 0))
    except (ValueError, TypeError):
        data.volume = 0

    try:
        data.repeat = int(s.get("repeat", "0"))
    except (ValueError, TypeError):
        data.repeat = 0

    try:
        data.position = float(s.get("secs", 0))
    except (ValueError, TypeError):
        data.position = None

    try:
        totlen = s.get("totlen")
        data.duration = float(totlen) if totlen else None
    except (ValueError, TypeError):
        data.duration = None

    return data


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
