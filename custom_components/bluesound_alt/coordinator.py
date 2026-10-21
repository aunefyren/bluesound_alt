"""DataUpdateCoordinator for Bluesound Alt."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
import logging
from typing import Any
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import xmltodict
from yarl import URL

from .const import (
    DOMAIN,
    LONG_POLL_MIN_INTERVAL,
    LONG_POLL_TIMEOUT,
    MAX_BROWSE_PAGES,
    NODE_OFFLINE_CHECK_TIMEOUT,
    RETRY_DELAY,
    SETTINGS_MENUS,
    SETTINGS_POLL_SECONDS,
    WEB_PORT,
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
    # Whether the player can jump within what is playing; live radio and
    # inputs cannot.
    can_seek: bool = False
    service: str | None = None
    service_name: str | None = None
    stream_url: str | None = None
    group_name: str | None = None
    sync_stat: str | None = None
    # Counters that move whenever the player's sources or presets change,
    # as the BluOS app watches them to know when to re-read its lists.
    source_list_id: str | None = None
    preset_list_id: str | None = None
    etag: str | None = None


@dataclass
class BluesoundSyncInfo:
    """Device identity and group topology from /SyncStatus."""

    name: str
    mac: str
    model: str | None = None
    # Who makes it: Bluesound, or NAD for a C700 or T758, say.
    brand: str | None = None
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
    # A receiver's own inputs (NAD T758 and the like), switched with
    # /ExternalSource: [{id, name, bluos}], and the id of the active one.
    # bluos marks the input that is the BluOS module itself.
    external_inputs: list[dict[str, Any]] = field(default_factory=list)
    external_input: str | None = None
    # Version of the /Settings schema the player speaks.
    schema_version: str | None = None
    etag: str | None = None


@dataclass
class BluesoundSetting:
    """One entry of the player's settings menus, as the BluOS app shows it.

    Changed by POSTing {name: value} as JSON to url, as the app does.
    """

    id: str
    name: str
    url: str
    # The app's control: boolean (value ON/OFF), list, range or dual-range.
    kind: str
    value: str | None
    # (value the player knows it by, label the app shows), for lists.
    options: list[tuple[str, str]] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    units: str | None = None
    # The least a dual-range's two ends may be apart.
    min_range: float | None = None
    # (name, value) of other settings this one only applies with, such as
    # bass only while tone controls are on.
    depends_on: list[tuple[str, str]] = field(default_factory=list)


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
        """Set up the coordinator for one player."""
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
        # A receiver's own inputs, kept current by long-polling /SyncStatus
        self.external_inputs: list[dict[str, Any]] = sync_info.external_inputs
        self.external_input: str | None = sync_info.external_input
        # Sources from /Browse: [{name, play_url}], re-read when /Status says
        # they changed (an input renamed or hidden in the BluOS app, say).
        self.sources: list[dict[str, str]] = []
        # Presets (saved radio/favourites) from /Presets: [{id, name, image}],
        # re-read likewise.
        self.presets: list[dict[str, str]] = []
        # Individual volume for this device (differs from group volume when slave)
        self.individual_volume: int | None = sync_info.volume
        self._session: aiohttp.ClientSession | None = None
        self._long_poll_task: asyncio.Task | None = None
        self._sync_poll_task: asyncio.Task | None = None
        self._settings_poll_task: asyncio.Task | None = None
        self._is_first_fetch = True
        # The Audio, Player and Sources settings menus, by setting name.
        self.settings: dict[str, BluesoundSetting] = {}

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the player, for every entity it has."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.sync_info.mac)},
            name=self.sync_info.name,
            model=self.sync_info.model,
            manufacturer=self.sync_info.brand or "Bluesound",
        )

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
                changed = etag is not None and info.etag != etag
                etag = info.etag
                self._apply_sync_info(info)
                if changed and self.settings:
                    # A new listening mode or volume limit moves /SyncStatus.
                    await self.async_refresh_settings()
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
        """Take on the grouping, volume and input a /SyncStatus answer reports."""
        before = (
            self.group_master_ip,
            self.group_master_port,
            self.group_slaves,
            self.individual_volume,
            self.external_inputs,
            self.external_input,
        )
        self.group_master_ip = info.master_ip
        self.group_master_port = info.master_port
        self.group_slaves = info.slaves
        if info.volume is not None:
            self.individual_volume = info.volume
        self.external_inputs = info.external_inputs
        self.external_input = info.external_input
        after = (
            self.group_master_ip,
            self.group_master_port,
            self.group_slaves,
            self.individual_volume,
            self.external_inputs,
            self.external_input,
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
            session = self._get_session()
            self.sources = await _fetch_sources(session, self.host, self.port)
            self.presets = await _fetch_presets(session, self.host, self.port)
            self.settings = await self._fetch_settings() or {}
            self._start_long_poll_loop()
            self._start_sync_poll_loop()
            if self.settings:
                self._settings_poll_task = self._start_background(
                    self._settings_poll_loop(), "settings"
                )

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
            await self._refresh_lists(self.data, data)
            self.async_set_updated_data(data)

    async def _refresh_lists(
        self, previous: BluesoundData | None, data: BluesoundData
    ) -> None:
        """Re-read the sources and presets when /Status says they changed."""
        if previous is None:
            return
        session = self._get_session()
        if data.source_list_id != previous.source_list_id:
            self.sources = await _fetch_sources(session, self.host, self.port)
        if data.preset_list_id != previous.preset_list_id:
            self.presets = await _fetch_presets(session, self.host, self.port)

    def source_options(self) -> list[tuple[str, str]]:
        """Return (key, name) for every selectable source, in list order.

        The key stays the same when the source is renamed, so a choice made
        about it, such as hiding it, survives the rename.
        """
        return [
            *((f"browse:{s['play_url']}", s["name"]) for s in self.sources),
            *((f"input:{i['id']}", i["name"]) for i in self.external_inputs),
            *((f"preset:{p['id']}", p["name"]) for p in self.presets),
        ]

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

    async def _fetch_settings(self) -> dict[str, BluesoundSetting] | None:
        """Read the settings menus; None if the player did not answer.

        A menu the player does not have is left out, so plain players simply
        offer fewer settings.
        """
        settings: dict[str, BluesoundSetting] = {}
        for menu in SETTINGS_MENUS:
            text = await self._fetch_settings_menu(menu)
            if text is None:
                return None
            try:
                for name, setting in _parse_settings(text).items():
                    settings.setdefault(name, setting)
            except ET.ParseError:
                _LOGGER.warning("Malformed %s settings from %s", menu, self.host)
                return None
        return settings

    async def _fetch_settings_menu(self, menu: str) -> str | None:
        """Read one settings menu: its XML, "" if it is missing, None on failure."""
        params = {"id": menu, "schemaVersion": self.sync_info.schema_version or "34"}
        try:
            async with self._get_session().get(
                f"{self._base_url()}/Settings",
                params=params,
                timeout=aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT),
            ) as resp:
                if resp.status == 404:
                    return ""
                if resp.status != 200:
                    return None
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            return None

    def setting_applies(self, setting: BluesoundSetting) -> bool:
        """Return whether the settings a setting depends on allow it now."""
        for name, value in setting.depends_on:
            other = self.settings.get(name)
            if other is not None and other.value != value:
                return False
        return True

    async def _settings_poll_loop(self) -> None:
        """Re-read the settings now and then, to follow the BluOS app.

        Changing the listening mode or volume limits moves /SyncStatus, which
        triggers a re-read too, but tone and EQ changes do not.
        """
        while True:
            await asyncio.sleep(SETTINGS_POLL_SECONDS)
            try:
                await self.async_refresh_settings()
            except Exception:
                # A dead loop would leave the settings stale for good.
                _LOGGER.exception("Unexpected error reading settings of %s", self.host)

    async def async_refresh_settings(self) -> None:
        """Re-read the settings, keeping the last ones if none come back."""
        settings = await self._fetch_settings()
        if settings is not None and settings != self.settings:
            self.settings = settings
            self.async_update_listeners()

    async def async_set_setting(self, name: str, value: str | float) -> None:
        """Change one setting, and show what the player then reports."""
        setting = self.settings[name]
        await async_send_command(
            self._get_session(),
            self._setting_url(setting.url),
            json={setting.name: value},
        )
        await self.async_refresh_settings()

    def _setting_url(self, url: str) -> URL:
        """Resolve a setting's URL, which players give relative or absolute.

        BluOS 4.18 and later name the player's own address in it. Home
        Assistant may reach the player by another name or address, so the
        configured host is kept and only the port and path are taken.
        """
        target = URL(url)
        if target.is_absolute():
            return target.with_host(self.host)
        return URL(self._base_url()).join(target)

    async def async_reboot(self) -> None:
        """Reboot the player, as the confirmation form on its web page does."""
        await async_send_command(
            self._get_session(),
            f"http://{self.host}:{WEB_PORT}/reboot",
            data={"noheader": "1", "yes": ""},
        )

    def stop(self) -> None:
        """Stop following the player."""
        for task in (
            self._long_poll_task,
            self._sync_poll_task,
            self._settings_poll_task,
        ):
            if task and not task.done():
                task.cancel()


async def async_send_command(
    session: aiohttp.ClientSession,
    url: str | URL,
    params: dict[str, Any] | None = None,
    *,
    json: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
) -> str:
    """Send a command to a player and return its answer, raising on refusal.

    A GET with query parameters, or a POST when there is a json or form body.
    """
    target = URL(url) if isinstance(url, str) else url
    host, command = target.host or "", target.path
    timeout = aiohttp.ClientTimeout(total=NODE_OFFLINE_CHECK_TIMEOUT)
    if json is not None or data is not None:
        request = session.post(url, json=json, data=data, timeout=timeout)
    else:
        request = session.get(url, params=params or None, timeout=timeout)
    try:
        async with request as resp:
            status = resp.status
            text = await resp.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise BluesoundUnreachableError(host, str(err) or type(err).__name__) from err

    # Settings answer 205 Reset Content: done, and re-read what is shown.
    if not 200 <= status < 300:
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

        # A receiver's own inputs: <externalSource id="4" name="Squeeze">
        # with one <item id=.. name=..> per input. Plain players have none.
        external = sync.get("externalSource")
        external_inputs: list[dict[str, Any]] = []
        external_input: str | None = None
        if isinstance(external, dict):
            external_input = external.get("@id")
            external_inputs = [
                {
                    "id": item["@id"],
                    "name": item.get("@name") or item["@id"],
                    "bluos": item.get("@isBluos") == "true",
                }
                for item in _as_list(external.get("item"))
                if isinstance(item, dict) and item.get("@id")
            ]

        volume = _safe_int(sync.get("@volume"), -1)
        return BluesoundSyncInfo(
            name=name,
            mac=mac,
            model=model,
            brand=sync.get("@brand") or None,
            ip=host,
            port=port,
            master_ip=master_ip,
            master_port=master_port,
            slaves=slaves,
            fixed_secondary=sync.get("@zoneSlave") == "true",
            volume=volume if volume >= 0 else None,
            external_inputs=external_inputs,
            external_input=external_input,
            schema_version=sync.get("@schemaVersion"),
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


# The controls entities are made from. Text (room and input names), icon
# lists and buttons are left out, which also keeps names out of diagnostics.
SETTING_KINDS = {"boolean", "list", "range", "dual-range"}


def _parse_settings(xml_text: str) -> dict[str, BluesoundSetting]:
    """Parse a settings menu into its settings, by name.

    A setting without a url of its own is changed through its menu's, as the
    app does with volume limits. A menu can itself be a setting, as the
    soundbar's subwoofer is. Only SETTING_KINDS are kept.
    """
    found: dict[str, BluesoundSetting] = {}

    def walk(element: ET.Element, url: str | None) -> None:
        url = element.get("url") or url
        for child in element:
            if child.tag not in ("menuGroup", "setting"):
                continue
            setting = _parse_setting(child, url)
            if setting is not None:
                found.setdefault(setting.name, setting)
            if child.tag == "menuGroup":
                walk(child, url)

    root = ET.fromstring(xml_text) if xml_text.strip() else None
    if root is not None:
        walk(root, None)
    return found


def _parse_setting(element: ET.Element, url: str | None) -> BluesoundSetting | None:
    """Parse one setting, or None if it is not one that can be set."""
    setting_id, name = element.get("id"), element.get("name")
    url = element.get("url") or url
    kind = element.get("class") or ""
    if not setting_id or not name or not url or kind not in SETTING_KINDS:
        return None
    setting = BluesoundSetting(
        id=setting_id, name=name, url=url, kind=kind, value=element.get("value")
    )
    for value in element.findall("value"):
        if value.get("name"):
            label = value.get("displayName") or value.get("name")
            setting.options.append((value.get("name"), label))
        elif value.get("min") is not None:
            setting.minimum = _safe_float(value.get("min"))
            setting.maximum = _safe_float(value.get("max"))
            setting.step = _safe_float(value.get("step"))
            setting.units = value.get("units")
            setting.min_range = _safe_float(value.get("minRange"))
    setting.depends_on = [
        (d.get("name"), d.get("value"))
        for d in element.findall("dependsOn")
        if d.get("name") and d.get("value") is not None
    ]
    return setting


def _safe_float(val: Any) -> float | None:
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_status(xml_text: str) -> BluesoundData:
    parsed = xmltodict.parse(xml_text)
    # An element with no attributes or children parses to None, not a dict.
    s = parsed.get("status") or {}

    data = BluesoundData()
    data.etag = s.get("@etag")
    data.state = s.get("state", "stop")
    data.muted = s.get("mute", "0") == "1"
    data.shuffle = s.get("shuffle", "0") == "1"
    data.can_seek = s.get("canSeek", "0") == "1"
    data.service = s.get("service")
    data.service_name = s.get("serviceName")
    data.title = s.get("title1")
    data.artist = s.get("title2")
    data.album = s.get("title3")
    data.image_url = s.get("image")
    data.group_name = s.get("groupName")
    data.sync_stat = s.get("syncStat")
    data.source_list_id = s.get("sid")
    data.preset_list_id = s.get("prid")
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
