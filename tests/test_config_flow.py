"""Tests for adding a player through the UI."""

from __future__ import annotations

from collections.abc import Iterator
from ipaddress import ip_address
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


# -- discovery --------------------------------------------------------------


def announcement(*addresses: str, port: int | None = MASTER[1]) -> ZeroconfServiceInfo:
    """Return a _musc._tcp announcement, as a BluOS player sends it."""
    ips = [ip_address(a) for a in addresses]
    return ZeroconfServiceInfo(
        ip_address=ips[0],
        ip_addresses=ips,
        port=port,
        hostname="player.local.",
        type="_musc._tcp.local.",
        name="Player._musc._tcp.local.",
        properties={},
    )


async def discover(hass: HomeAssistant, info: ZeroconfServiceInfo) -> dict:
    """Start a flow the way Home Assistant does when a player announces itself."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )


async def test_discovered_player_is_offered(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A new player is offered by name, and added once confirmed."""
    result = await discover(hass, announcement(MASTER[0]))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "name": "Player 1",
        "host": MASTER[0],
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Player 1"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == MASTER_MAC


async def test_discovery_updates_a_moved_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A known player announcing a new address is updated, not offered again."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MASTER_MAC,
        data={CONF_HOST: "192.0.2.99", CONF_PORT: MASTER[1]},
    )
    entry.add_to_hass(hass)

    result = await discover(hass, announcement(MASTER[0]))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == MASTER[0]


async def test_discovery_prefers_ipv4(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """An announcement listing IPv6 first is reached on its IPv4 address."""
    result = await discover(hass, announcement("2001:db8::1", MASTER[0]))

    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["host"] == MASTER[0]


async def test_discovery_without_ipv4(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player announcing only IPv6 is not offered."""
    result = await discover(hass, announcement("2001:db8::1"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ipv4_address"


async def test_discovered_player_unreachable(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player that announces itself but does not answer is not offered."""
    patch_session.player(MASTER).offline = True

    result = await discover(hass, announcement(MASTER[0]))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_discovery_without_port(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """An announcement without a port is tried on the default one."""
    result = await discover(hass, announcement(MASTER[0], port=None))

    assert result["step_id"] == "confirm"


async def test_user_step_updates_a_moved_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Adding a known player again at its new address updates the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MASTER_MAC,
        data={CONF_HOST: "192.0.2.99", CONF_PORT: MASTER[1]},
    )
    entry.add_to_hass(hass)

    result = await submit(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == MASTER[0]
