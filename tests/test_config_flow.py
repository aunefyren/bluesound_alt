"""Tests for adding a player through the UI."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.bluesound_alt.const import DOMAIN

from .conftest import FakeBluOS, config_entry_for
from .const import MASTER, MASTER_MAC

USER_INPUT = {CONF_HOST: MASTER[0], CONF_PORT: MASTER[1]}


@pytest.fixture(autouse=True)
def skip_entry_setup() -> Iterator[None]:
    """Keep created entries from setting up; that is tested elsewhere."""
    with patch("custom_components.bluesound_alt.async_setup_entry", return_value=True):
        yield


async def submit(hass: HomeAssistant) -> dict:
    """Open the user step and submit the master's address."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)


async def test_creates_entry(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A reachable player is added under its own name and MAC."""
    result = await submit(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Player 1"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == MASTER_MAC


async def test_already_configured(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The same player cannot be added twice."""
    config_entry_for(MASTER, MASTER_MAC).add_to_hass(hass)

    result = await submit(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_offline_player(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """An unreachable address is a connection problem."""
    patch_session.player(MASTER).offline = True

    result = await submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_player_times_out(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """An address nothing answers on is a connection problem, not unknown."""
    patch_session.player(MASTER).timing_out = True

    result = await submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_http_error(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """Something that answers, but not like a player, is an invalid response."""
    patch_session.player(MASTER).overrides[("/SyncStatus", frozenset())] = (404, "")

    result = await submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_response"}


async def test_not_xml(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A web server that is not a player is an invalid response, not unknown."""
    patch_session.player(MASTER).overrides[("/SyncStatus", frozenset())] = (
        200,
        "<html><body>router login",
    )

    result = await submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_response"}


async def test_recovers_after_error(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Fixing the problem and submitting again adds the player."""
    patch_session.player(MASTER).offline = True
    result = await submit(hass)
    assert result["errors"] == {"base": "cannot_connect"}

    patch_session.player(MASTER).offline = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
