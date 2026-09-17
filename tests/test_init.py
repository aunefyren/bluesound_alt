"""Tests for setting up and unloading players."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .conftest import FakeBluOS, config_entry_for, setup_player
from .const import MASTER, MASTER_MAC


async def test_setup_and_unload(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A reachable player loads, shows its state, and unloads cleanly."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)

    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get("media_player.player_1")
    assert state is not None
    assert state.state == "playing"
    assert state.attributes["media_title"] == "PlexAmp"

    coordinator = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    # The push loop is stopped, not left talking to the player.
    assert coordinator._long_poll_task is None or coordinator._long_poll_task.done()


async def test_setup_retries_when_player_offline(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player that is off at startup is retried, not given up on."""
    patch_session.player(MASTER).offline = True
    entry = config_entry_for(MASTER, MASTER_MAC)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_when_player_times_out(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player that does not answer at startup is retried as well."""
    patch_session.player(MASTER).timing_out = True
    entry = config_entry_for(MASTER, MASTER_MAC)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
