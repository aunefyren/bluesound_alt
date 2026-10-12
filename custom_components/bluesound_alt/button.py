"""Bluesound Alt buttons: restarting a player."""
from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BluesoundConfigEntry
from .coordinator import BluesoundCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluesoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the restart button of a player."""
    async_add_entities([BluesoundRestartButton(entry.runtime_data)])


class BluesoundRestartButton(CoordinatorEntity[BluesoundCoordinator], ButtonEntity):
    """Reboot the player, as Diagnostics → Reboot in the BluOS app does."""

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BluesoundCoordinator) -> None:
        """Set up the restart button of one player."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.sync_info.mac}_restart"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        """Reboot the player; it shows as unavailable until it is back."""
        await self.coordinator.async_reboot()
