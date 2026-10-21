"""Tests for the sleep timer.

Players step their timer with /Sleep: off, 15, 30, 45, 60, 90, then off
again, and from a running timer to the next step above the minutes left.
/Status shows the minutes left, counting down. Seen on BluOS 4.18.18.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations
import pytest

from custom_components.bluesound_alt.const import DOMAIN

from .conftest import FakeBluOS, setup_player, wait_for
from .const import MASTER, MASTER_MAC

SLEEP = "select.player_1_sleep_timer"


async def pick(hass: HomeAssistant, option: str) -> None:
    """Choose a sleep timer option."""
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": SLEEP, "option": option},
        blocking=True,
    )


def sleeps(patch_session: FakeBluOS) -> int:
    """Return how many times /Sleep was sent."""
    return sum(r.path == "/Sleep" for r in patch_session.requests)


async def test_off_to_begin_with(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """With no timer running the timer is off, with its steps to pick from."""
    await setup_player(hass, MASTER, MASTER_MAC)

    state = hass.states.get(SLEEP)
    assert state.state == "off"
    assert state.attributes["options"] == ["off", "15", "30", "45", "60", "90"]
    assert state.attributes["minutes_left"] is None


async def test_set_a_step(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A step is reached by stepping the player's timer, and shown at once."""
    await setup_player(hass, MASTER, MASTER_MAC)

    await pick(hass, "45")

    assert sleeps(patch_session) == 3
    state = hass.states.get(SLEEP)
    assert (state.state, state.attributes["minutes_left"]) == ("45", 45)


async def test_turn_off(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """Off steps on past 90; when already off, nothing is sent."""
    await setup_player(hass, MASTER, MASTER_MAC)
    await pick(hass, "60")

    await pick(hass, "off")
    assert hass.states.get(SLEEP).state == "off"
    assert patch_session.player(MASTER).sleep is None

    before = sleeps(patch_session)
    await pick(hass, "off")
    assert sleeps(patch_session) == before


async def test_counting_down(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """While it runs, the step it counts down from shows, and the minutes left."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)

    player.push(
        "/Status",
        player.captured("status_long_poll").replace(
            "<sleep></sleep>", "<sleep>14</sleep>"
        ),
    )
    await wait_for(lambda: hass.states.get(SLEEP).attributes["minutes_left"] == 14)
    assert hass.states.get(SLEEP).state == "15"

    # Picking it again starts it over: from 14 left, one step is 15.
    player.sleep = 14
    before = sleeps(patch_session)
    await pick(hass, "15")
    assert sleeps(patch_session) == before + 1
    assert hass.states.get(SLEEP).attributes["minutes_left"] == 15


async def test_player_that_does_not_step(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A timer that never reaches the step asked for fails, after one round."""
    await setup_player(hass, MASTER, MASTER_MAC)
    patch_session.player(MASTER).overrides[("/Sleep", frozenset())] = (
        200,
        "<sleep>15</sleep>",
    )

    with pytest.raises(HomeAssistantError) as raised:
        await pick(hass, "30")
    assert raised.value.translation_key == "sleep_not_set"
    assert sleeps(patch_session) == 6


async def test_norwegian_names(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """The Norwegian translation loads, sleep timer and all."""
    await setup_player(hass, MASTER, MASTER_MAC)

    names = await async_get_translations(hass, "nb", "entity", {DOMAIN})
    prefix = f"component.{DOMAIN}.entity.select.sleep_timer"
    assert names[f"{prefix}.name"] == "Innsovningstimer"
    assert names[f"{prefix}.state.off"] == "Av"
    assert names[f"component.{DOMAIN}.entity.number.treble.name"] == "Diskant"
