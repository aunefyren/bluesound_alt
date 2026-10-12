"""Bluesound Alt select entities: listening mode, output mode and the like."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluesoundConfigEntry
from .coordinator import BluesoundCoordinator
from .entity import BluesoundSettingEntity

# Setting name -> (translation key, entity category).
SELECTS: dict[str, tuple[str, EntityCategory | None]] = {
    "preset": ("listening_mode", None),
    "replayGainMode": ("replay_gain", EntityCategory.CONFIG),
    "channelMode": ("output_mode", EntityCategory.CONFIG),
    "bluetoothAutoplay": ("bluetooth_mode", EntityCategory.CONFIG),
    "ledbrightness": ("indicator_brightness", EntityCategory.CONFIG),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluesoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the choices the player's menus offer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BluesoundSettingSelect(coordinator, name, key, category)
        for name, (key, category) in SELECTS.items()
        if (s := coordinator.settings.get(name)) and s.kind == "list" and s.options
    )


class BluesoundSettingSelect(BluesoundSettingEntity, SelectEntity):
    """A list setting, its choices labelled as in the BluOS app."""

    def __init__(
        self,
        coordinator: BluesoundCoordinator,
        name: str,
        key: str,
        category: EntityCategory | None,
    ) -> None:
        """Set up one list setting."""
        super().__init__(coordinator, name, key)
        self._attr_entity_category = category

    @property
    def options(self) -> list[str]:
        """Return the choices by their labels."""
        return [label for _, label in self.setting.options] if self.setting else []

    @property
    def current_option(self) -> str | None:
        """Return the label of the active choice."""
        if (setting := self.setting) is None:
            return None
        return next((lb for n, lb in setting.options if n == setting.value), None)

    async def async_select_option(self, option: str) -> None:
        """Switch to a choice by its label."""
        await self._set(next(n for n, lb in self.setting.options if lb == option))
