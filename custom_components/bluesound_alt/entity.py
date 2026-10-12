"""Base for Bluesound Alt entities that show one of a player's audio settings."""
from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import BluesoundCoordinator, BluesoundSetting


class BluesoundSettingEntity(CoordinatorEntity[BluesoundCoordinator]):
    """One setting from the player's settings menus.

    Unavailable while the menu leaves the setting out, as it may with tone and
    EQ in some listening modes, and while a setting it depends on rules it
    out, as tone controls being off does for bass and treble.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BluesoundCoordinator, setting_name: str, key: str
    ) -> None:
        """Follow one setting, named by its translation key."""
        super().__init__(coordinator)
        self._setting_name = setting_name
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.sync_info.mac}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def setting(self) -> BluesoundSetting | None:
        """Return the setting as last read, if the menu still lists it."""
        return self.coordinator.settings.get(self._setting_name)

    @property
    def available(self) -> bool:
        """Return whether the player lists the setting and it applies now."""
        setting = self.setting
        return (
            super().available
            and setting is not None
            and self.coordinator.setting_applies(setting)
        )

    async def _set(self, value: str | float) -> None:
        await self.coordinator.async_set_setting(self._setting_name, value)
