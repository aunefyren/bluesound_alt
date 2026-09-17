"""DataUpdateCoordinator for Bluesound Alt."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
import logging
from typing import Any
from xml.parsers.expat import ExpatError

import aiohttp
import xmltodict
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    LONG_POLL_MIN_INTERVAL,
    LONG_POLL_TIMEOUT,
    MAX_BROWSE_PAGES,
    NODE_OFFLINE_CHECK_TIMEOUT,
    RETRY_DELAY,
)

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
    master_port: int = 11000
    slaves: list[dict[str, Any]] = field(default_factory=list)
    # The secondary player of a fixed group (a stereo pair, say), which only
    # moves with its primary and must not be regrouped on its own.
    fixed_secondary: bool = False
    # This player's own volume (0..100), None for fixed volume or unknown.
    volume: int | None = None
    etag: str | None = None


class BluesoundCommandError(HomeAssistantError):
    """A player refused a command.

    Players answer most failures with HTTP 200 and an <error> body, so the
    reason is kept for callers that can recover from particular refusals.
    """

    def __init__(self, host: str, command: str, reason: str) -> None:
        """Describe which player refused which command, and why."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={
                "host": host,
                "command": command,
                "reason": reason,
            },
        )
        self.reason = reason


class BluesoundUnreachableError(HomeAssistantError):
    """A player did not answer a command."""

    def __init__(self, host: str, error: str) -> None:
        """Describe which player could not be reached."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="cannot_reach",
            translation_placeholders={"host": host, "error": error},
        )


class BluesoundCoordinator(DataUpdateCoordinator[BluesoundData]):
    """Coordinator for a single Bluesound player."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        host: str,
        port: int,
        sync_info: BluesoundSyncInfo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{host}",
            update_interval=None,
        )
        self.host = host
        self.port = port
        self.sync_info = sync_info
        # Group topology, kept current by long-polling /SyncStatus
        self.group_master_ip: str | None = sync_info.master_ip
        self.group_master_port: int = sync_info.master_port
        self.group_slaves: list[dict[str, Any]] = sync_info.slaves
        # Sources fetched once from /Browse: [{name, play_url}]
        self.sources: list[dict[str, str]] = []
        # Presets (saved radio/favourites) fetched once from /Presets: [{id, name, image}]
        self.presets: list[dict[str, str]] = []
        # Individual volume for this device (differs from group volume when slave)
        self.individual_volume: int | None = sync_info.volume
        self._session: aiohttp.ClientSession | None = None
        self._long_poll_task: asyncio.Task | None = None
        self._sync_poll_task: asyncio.Task | None = None
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

        try:
            return _parse_status(text)
        except ExpatError as err:
            raise UpdateFailed(f"Malformed status from {url}: {err}") from err

    def _start_background(
        self, target: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Run a loop for as long as the player is set up.

        A background task, so Home Assistant does not wait on it as if it were
        setup work, and it is cancelled when the entry unloads.
        """
        return self.config_entry.async_create_background_task(
            self.hass, target, name=f"{DOMAIN}_{name}_{self.host}"
        )

    def _start_sync_poll_loop(self) -> None:
        if self._sync_poll_task and not self._sync_poll_task.done():
            return
        self._sync_poll_task = self._start_background(self._sync_poll_loop(), "sync")

    async def _sync_poll_loop(self) -> None:
        """Follow /SyncStatus: who this player is grouped with, and its volume.

        Long-polled, as the BluOS API recommends for grouping and per-player
        volume. A leader reports a regroup over several updates, and the
        syncStat hint in /Status does not reliably come with the last of them,
        so /SyncStatus itself is followed rather than re-read on hints.
        """
        loop = asyncio.get_running_loop()
        etag: str | None = None
        while True:
            started = loop.time()
            try:
                info = await _fetch_sync_info(
                    self._get_session(), self.host, self.port, etag=etag
                )
                if info is None:
                    etag = None
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                etag = info.etag
                self._apply_sync_info(info)
            except Exception:
                # A dead loop would freeze grouping and volume for good.
                _LOGGER.exception("Unexpected error following %s", self.host)
                etag = None
                await asyncio.sleep(RETRY_DELAY)
                continue
            # The API asks for at least a second between long polls of one
            # resource, even when an answer comes back sooner.
            if (wait := LONG_POLL_MIN_INTERVAL - (loop.time() - started)) > 0:
                await asyncio.sleep(wait)

    @callback
    def _apply_sync_info(self, info: BluesoundSyncInfo) -> None:
        """Take on the grouping and volume a /SyncStatus answer reports."""
        before = (
            self.group_master_ip,
            self.group_master_port,
            self.group_slaves,
            self.individual_volume,
        )
        self.group_master_ip = info.master_ip
        self.group_master_port = info.master_port
        self.group_slaves = info.slaves
        if info.volume is not None:
            self.individual_volume = info.volume
        after = (
            self.group_master_ip,
            self.group_master_port,
            self.group_slaves,
            self.individual_volume,
        )
        if after == before:
            return
        _LOGGER.debug(
            "%s group topology: master=%s slaves=%s",
            self.host,
            self.group_master_ip,
            [s["ip"] for s in self.group_slaves],
        )
        if self.data is not None:
            self.async_set_updated_data(self.data)

    @callback
    def async_apply_volume_answer(self, answer: str) -> None:
        """Show the volume and mute a /Volume answer reports, straight away.

        /Status and /SyncStatus catch up a moment later; until then the level
        just set would otherwise flash back to the previous one.
        """
        try:
            volume = xmltodict.parse(answer).get("volume")
        except ExpatError:
            return
        if not isinstance(volume, dict):
            return
        level = _safe_int(volume.get("#text"), -1)
        muted = volume.get("@mute") == "1"
        if level < 0 or self.data is None:
            return
        if self.group_master_ip:
            # A follower's /Status is its leader's; only its own level changed.
            self.individual_volume = level
            self.async_set_updated_data(self.data)
        else:
            self.async_set_updated_data(replace(self.data, volume=level, muted=muted))

    async def _async_update_data(self) -> BluesoundData:
        try:
            data = await self._fetch_status(etag=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Cannot connect to {self.host}: {err}") from err

        if self._is_first_fetch:
            self._is_first_fetch = False
            self.sources = await _fetch_sources(self._get_session(), self.host, self.port)
            self.presets = await _fetch_presets(self._get_session(), self.host, self.port)
            self._start_long_poll_loop()
            self._start_sync_poll_loop()

        return data

    def _start_long_poll_loop(self) -> None:
        if self._long_poll_task and not self._long_poll_task.done():
            return
        self._long_poll_task = self._start_background(self._long_poll_loop(), "poll")

    async def _long_poll_loop(self) -> None:
        """Follow the player's state until the entry unloads.

        Any failure marks the player unavailable and retries after a pause;
        the loop only ends when it is cancelled.
        """
        etag: str | None = self.data.etag if self.data else None

        while True:
            try:
                data = await self._fetch_status(etag=etag)
            except (aiohttp.ClientError, TimeoutError, UpdateFailed) as err:
                _LOGGER.debug("Long-poll error for %s: %s, retrying", self.host, err)
                self.async_set_update_error(err)
                etag = None
                await asyncio.sleep(RETRY_DELAY)
                continue
            except Exception as err:
                _LOGGER.exception("Unexpected error polling %s", self.host)
                self.async_set_update_error(err)
                etag = None
                await asyncio.sleep(RETRY_DELAY)
                continue

            etag = data.etag
            self.async_set_updated_data(data)

    async def async_request_api(self, path: str, **params: Any) -> str:
        """Send a command to the player and return its answer.

        Raises BluesoundCommandError when the player refuses it, and
        BluesoundUnreachableError when it does not answer.
        """
        return await async_send_command(
            self._get_session(), f"{self._base_url()}{path}", params
        )

    async def async_browse(self, key: str | None = None) -> list[dict[str, str | None]]:
        """Fetch /Browse (optionally for a browseKey) and return parsed items.

        Long menus come a page at a time, each pointing to the next with a
        nextKey; pages are followed up to MAX_BROWSE_PAGES.
        """
        items: list[dict[str, str | None]] = []
        for _ in range(MAX_BROWSE_PAGES):
            page = await self._browse_page(key)
            if page is None:
                break
            page_items, key = page
            items.extend(page_items)
            if not key:
                break
        return items

    async def _browse_page(
        self, key: str | None
    ) -> tuple[list[dict[str, str | None]], str | None] | None:
        """Fetch one page of a browse menu, returning its items and nextKey."""
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
                    return None
                text = await resp.text()
            return _parse_browse(text)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Browse %s failed: %s", key, err)
        except ExpatError as err:
            _LOGGER.error("Browse %s returned malformed XML: %s", key, err)
        return None

    async def async_play_path(self, path: str) -> None:
        """GET a device-provided relative URL (e.g. a browse playURL) verbatim.

        The path is already percent-encoded by the device, so it is sent
        unchanged (encoded=True) rather than decoded and rebuilt.
        """
        await async_send_command(
            self._get_session(), URL(f"{self._base_url()}{path}", encoded=True)
        )

    def stop(self) -> None:
        for task in (self._long_poll_task, self._sync_poll_task):
            if task and not task.done():
                task.cancel()


async def async_send_command(
    session: aiohttp.ClientSession,
    url: str | URL,
    params: dict[str, Any] | None = None,
) -> str:
    """Send a command to a player and return its answer, raising on refusal."""
    target = URL(url) if isinstance(url, str) else url
    host, command = target.host or "", target.path
    try:
        async with session.get(
            url,
            params=params or None,
            timeout=aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT),
        ) as resp:
            status = resp.status
            text = await resp.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise BluesoundUnreachableError(host, str(err) or type(err).__name__) from err

    if status != 200:
        raise BluesoundCommandError(host, command, f"HTTP {status}")
    if (reason := _error_reason(text)) is not None:
        raise BluesoundCommandError(host, command, reason)
    return text


def _error_reason(text: str) -> str | None:
    """Return the reason from an <error> answer, or None for anything else."""
    try:
        parsed = xmltodict.parse(text)
    except ExpatError:
        return None
    if not isinstance(parsed, dict) or "error" not in parsed:
        return None
    error = parsed["error"]
    if isinstance(error, dict):
        error = error.get("#text")
    return (error or "").strip() or "unknown error"


async def _fetch_sync_info(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    etag: str | None = None,
) -> BluesoundSyncInfo | None:
    """Fetch /SyncStatus and return device identity + group topology.

    With an etag, long-polls: the player answers once something changes.
    """
    url = f"http://{host}:{port}/SyncStatus"
    if etag:
        params: dict[str, Any] = {"timeout": LONG_POLL_TIMEOUT, "etag": etag}
        request_timeout = LONG_POLL_TIMEOUT + NODE_OFFLINE_CHECK_TIMEOUT
    else:
        params = {}
        request_timeout = NODE_OFFLINE_CHECK_TIMEOUT
    try:
        async with session.get(
            url,
            params=params or None,
            timeout=aiohttp.ClientTimeout(total=request_timeout),
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except (aiohttp.ClientError, TimeoutError):
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
        master_port = 11000
        master_raw = sync.get("master")
        if master_raw:
            if isinstance(master_raw, dict):
                master_ip = master_raw.get("#text") or None
                master_port = _safe_int(master_raw.get("@port"), 11000)
            elif isinstance(master_raw, str) and master_raw.strip():
                master_ip = master_raw.strip()

        return BluesoundSyncInfo(
            name=name,
            mac=mac,
            model=model,
            ip=host,
            port=port,
            master_ip=master_ip,
            master_port=master_port,
            slaves=slaves,
            fixed_secondary=sync.get("@zoneSlave") == "true",
            volume=volume if (volume := _safe_int(sync.get("@volume"), -1)) >= 0 else None,
            etag=sync.get("@etag"),
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
    except (aiohttp.ClientError, TimeoutError):
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
    except (aiohttp.ClientError, TimeoutError):
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


def _as_list(value: Any) -> list[Any]:
    """Normalise xmltodict's one-or-many child elements to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _parse_browse_items(xml_text: str) -> list[dict[str, str | None]]:
    """Parse a /Browse response into [{name, image, browse_key, play_url}]."""
    return _parse_browse(xml_text)[0]


def _parse_browse(
    xml_text: str,
) -> tuple[list[dict[str, str | None]], str | None]:
    """Parse one /Browse page into its items and the key of the next page.

    play_url is the device-provided relative URL (e.g. /Play?url=...) kept
    verbatim so it can be replayed exactly via async_play_path().
    """
    parsed = xmltodict.parse(xml_text)
    root = parsed.get("browse") or parsed.get("radiotime") or {}

    # Menus either list items directly or group them into categories
    # ("Recents", "MQA"), which are flattened in order.
    items = _as_list(root.get("item"))
    for category in _as_list(root.get("category")):
        if isinstance(category, dict):
            items.extend(_as_list(category.get("item")))

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
    return result, root.get("@nextKey") or None


def _parse_status(xml_text: str) -> BluesoundData:
    parsed = xmltodict.parse(xml_text)
    # An element with no attributes or children parses to None, not a dict.
    s = parsed.get("status") or {}

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
