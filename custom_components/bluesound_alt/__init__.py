"""Bluesound Alt integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import BluesoundCoordinator, BluesoundSyncInfo, _fetch_sync_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    session = async_get_clientsession(hass)
    sync_info = await _fetch_sync_info(session, host, port)
    if sync_info is None:
        _LOGGER.error("Could not fetch SyncStatus from %s:%s", host, port)
        return False

    coordinator = BluesoundCoordinator(hass, host, port, sync_info)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: BluesoundCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.stop()
    return unloaded
