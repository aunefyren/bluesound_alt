"""The Bluesound component."""
from .media_player import (async_setup_platform)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
import logging

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry for Bluesound."""
    _LOGGER.debug("Starting entry: %s", entry)

    await async_setup_platform(hass, entry, AddEntitiesCallback, None)
    return True