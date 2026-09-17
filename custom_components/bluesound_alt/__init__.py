"""Bluesound Alt integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_PORT
from .coordinator import BluesoundCoordinator, _fetch_sync_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]

type BluesoundConfigEntry = ConfigEntry[BluesoundCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BluesoundConfigEntry) -> bool:
    """Set up a player, or have Home Assistant retry until it is reachable."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    session = async_get_clientsession(hass)
    sync_info = await _fetch_sync_info(session, host, port)
    if sync_info is None:
        # Players are often off or asleep when Home Assistant starts.
        raise ConfigEntryNotReady(f"Could not fetch SyncStatus from {host}:{port}")

    coordinator = BluesoundCoordinator(hass, entry, host, port, sync_info)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BluesoundConfigEntry) -> bool:
    """Unload a player and stop talking to it."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.stop()
    return unloaded
