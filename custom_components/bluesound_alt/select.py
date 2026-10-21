"""Bluesound Alt select entities: listening mode, output mode and the like."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BluesoundConfigEntry
from .const import SLEEP_STEPS
from .coordinator import BluesoundCoordinator
from .entity import BluesoundSettingEntity

SLEEP_OFF = "off"

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
        [
            BluesoundSleepTimer(coordinator),
            *(
                BluesoundSettingSelect(coordinator, name, key, category)
                for name, (key, category) in SELECTS.items()
                if (s := coordinator.settings.get(name))
                and s.kind == "list"
                and s.options
            ),
        ]
    )


class BluesoundSleepTimer(CoordinatorEntity[BluesoundCoordinator], SelectEntity):
    """The sleep timer: off, or one of its steps.

    While it runs, the step shown is the one the minutes left count down
    from, and the minutes left are an attribute. In a group the timer is the
    group's, set from any of its players.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "sleep_timer"
    def __init__(self, coordinator: BluesoundCoordinator) -> None:
        """Follow one player's sleep timer."""
        super().__init__(coordinator)
        self._attr_options = [SLEEP_OFF, *(str(step) for step in SLEEP_STEPS)]
        self._attr_unique_id = f"{coordinator.sync_info.mac}_sleep_timer"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str | None:
        """Return off, or the step the timer is counting down from."""
        minutes = self.coordinator.data.sleep
        if minutes is None:
            return SLEEP_OFF
        return str(next((s for s in SLEEP_STEPS if s >= minutes), SLEEP_STEPS[-1]))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the minutes left, while the timer runs."""
        return {"minutes_left": self.coordinator.data.sleep}

    async def async_select_option(self, option: str) -> None:
        """Set the timer to a step, or turn it off.

        Picking the step already running starts it over.
        """
        await self.coordinator.async_set_sleep(
            None if option == SLEEP_OFF else int(option)
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
