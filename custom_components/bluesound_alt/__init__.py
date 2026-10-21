"""Bluesound Alt integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import BluesoundCoordinator, _fetch_sync_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

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
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: BluesoundConfigEntry
) -> None:
    """Show changed options, such as hidden sources, without a reload.

    Every player is told, not just this one: a player in a group follows its
    leader's linked inputs.
    """
    for other in hass.config_entries.async_loaded_entries(DOMAIN):
        other.runtime_data.async_update_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: BluesoundConfigEntry) -> bool:
    """Unload a player and stop talking to it."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.stop()
    return unloaded
