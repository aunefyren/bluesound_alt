"""Bluesound Alt switches: on/off settings such as tone controls and EQ."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluesoundConfigEntry
from .coordinator import BluesoundCoordinator, BluesoundSetting
from .entity import BluesoundSettingEntity

# Setting name -> (translation key, entity category).
SWITCHES: dict[str, tuple[str, EntityCategory | None]] = {
    "eq-switch": ("tone_controls", None),
    "eq-upmix": ("surround_upmixer", EntityCategory.CONFIG),
    "eq-virt": ("virtualizer", EntityCategory.CONFIG),
    "eq-vlamp": ("volume_leveler", EntityCategory.CONFIG),
    "eq-excursion2": ("deep_bass", EntityCategory.CONFIG),
    "subwoofer": ("subwoofer", EntityCategory.CONFIG),
    "auto-lcd-brightness": ("auto_display_brightness", EntityCategory.CONFIG),
    "darkmode": ("dark_mode", EntityCategory.CONFIG),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluesoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the on/off settings the player's menus offer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BluesoundSettingSwitch(coordinator, name, key, category)
        for name, (key, category) in SWITCHES.items()
        if (s := coordinator.settings.get(name)) and _states(s)
    )


def _states(setting: BluesoundSetting) -> tuple[str, str] | None:
    """Return the (off, on) values of an on/off setting, or None if it is not one.

    Most spell them OFF and ON. Some list their own, as the subwoofer does
    with Off = "default" and On = "withsub".
    """
    if setting.kind != "boolean":
        return None
    if not setting.options:
        return ("OFF", "ON")
    if len(setting.options) == 2:
        return (setting.options[0][0], setting.options[1][0])
    return None


class BluesoundSettingSwitch(BluesoundSettingEntity, SwitchEntity):
    """An on/off setting."""

    def __init__(
        self,
        coordinator: BluesoundCoordinator,
        name: str,
        key: str,
        category: EntityCategory | None,
    ) -> None:
        """Set up one on/off setting."""
        super().__init__(coordinator, name, key)
        self._attr_entity_category = category

    @property
    def is_on(self) -> bool | None:
        """Return whether the setting is on."""
        if (setting := self.setting) is None or (states := _states(setting)) is None:
            return None
        return setting.value == states[1]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        if self.setting and (states := _states(self.setting)):
            await self._set(states[1])

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        if self.setting and (states := _states(self.setting)):
            await self._set(states[0])
