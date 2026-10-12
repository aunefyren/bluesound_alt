"""Bluesound Alt numbers: tone, balance, display brightness and volume limits."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluesoundConfigEntry
from .const import DOMAIN
from .coordinator import BluesoundCoordinator
from .entity import BluesoundSettingEntity

# Setting name -> (translation key, entity category), for single ranges.
RANGES: dict[str, tuple[str, EntityCategory | None]] = {
    "eq-bass": ("bass", None),
    "eq-treble": ("treble", None),
    "eq-balance": ("balance", None),
    "lcd-brightness": ("display_brightness", EntityCategory.CONFIG),
}
# A dual range, "low,high", shown as one number per end.
VOLUME_LIMITS = "volumeLimits"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluesoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the ranges the player's menus offer."""
    coordinator = entry.runtime_data
    settings = coordinator.settings
    entities: list[NumberEntity] = [
        BluesoundRange(coordinator, name, key, category)
        for name, (key, category) in RANGES.items()
        if (s := settings.get(name)) and s.kind == "range"
    ]
    if (s := settings.get(VOLUME_LIMITS)) and s.kind == "dual-range":
        entities += [
            BluesoundVolumeLimit(coordinator, "volume_limit_min", 0),
            BluesoundVolumeLimit(coordinator, "volume_limit_max", 1),
        ]
    async_add_entities(entities)


def _json_number(value: float) -> int | float:
    """Send whole numbers as the app does: 0, not 0.0."""
    return int(value) if float(value).is_integer() else value


class BluesoundRange(BluesoundSettingEntity, NumberEntity):
    """A level such as bass or balance, in the steps and range the player allows."""

    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: BluesoundCoordinator,
        name: str,
        key: str,
        category: EntityCategory | None,
    ) -> None:
        """Set up one range setting."""
        super().__init__(coordinator, name, key)
        self._attr_entity_category = category

    @property
    def native_value(self) -> float | None:
        """Return the level."""
        try:
            return float(self.setting.value)
        except (AttributeError, TypeError, ValueError):
            return None

    @property
    def native_min_value(self) -> float:
        """Return the lowest level."""
        return _bound(self.setting, "minimum", 0.0)

    @property
    def native_max_value(self) -> float:
        """Return the highest level."""
        return _bound(self.setting, "maximum", 100.0)

    @property
    def native_step(self) -> float:
        """Return the step between levels."""
        return _bound(self.setting, "step", 1.0)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the player gives, such as dB or %."""
        return self.setting.units if self.setting else None

    async def async_set_native_value(self, value: float) -> None:
        """Set the level."""
        await self._set(_json_number(value))


class BluesoundVolumeLimit(BluesoundSettingEntity, NumberEntity):
    """One end of the volume limits: the quietest or loudest volume allowed."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1.0

    def __init__(self, coordinator: BluesoundCoordinator, key: str, end: int) -> None:
        """Follow the low (0) or high (1) end of the volume limits."""
        super().__init__(coordinator, VOLUME_LIMITS, key)
        self._end = end

    def _limits(self) -> list[float] | None:
        try:
            low, high = (float(v) for v in self.setting.value.split(","))
        except (AttributeError, TypeError, ValueError):
            return None
        return [low, high]

    @property
    def native_value(self) -> float | None:
        """Return this end's limit."""
        limits = self._limits()
        return limits[self._end] if limits else None

    @property
    def native_min_value(self) -> float:
        """Return the lowest limit the player allows."""
        return _bound(self.setting, "minimum", -90.0)

    @property
    def native_max_value(self) -> float:
        """Return the highest limit the player allows."""
        return _bound(self.setting, "maximum", 0.0)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, dB."""
        return self.setting.units if self.setting else None

    async def async_set_native_value(self, value: float) -> None:
        """Move this end, keeping the gap between the ends the player needs."""
        limits = self._limits()
        if limits is None:
            return
        limits[self._end] = value
        gap = self.setting.min_range or 0
        if limits[1] - limits[0] < gap:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="volume_limits_too_close",
                translation_placeholders={"gap": f"{gap:g}"},
            )
        await self._set(",".join(f"{_json_number(v)}" for v in limits))


def _bound(setting, attribute: str, default: float) -> float:
    value = getattr(setting, attribute, None) if setting else None
    return value if value is not None else default
