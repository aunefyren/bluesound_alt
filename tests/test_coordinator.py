"""Tests for the push loop that keeps a player's state current."""

from __future__ import annotations

import re

from homeassistant.core import HomeAssistant

from .conftest import FakeBluOS, fixture_text, setup_player, wait_for
from .const import LABEL, MASTER, MASTER_MAC, SLAVE_1, SLAVE_1_MAC

ENTITY = "media_player.player_1"


def status_titled(title: str) -> str:
    """Return the master's captured status with another title."""
    return fixture_text(LABEL, MASTER, "status").replace(
        "<title1>PlexAmp</title1>", f"<title1>{title}</title1>"
    )


def title(hass: HomeAssistant) -> str | None:
    """Return the title the entity currently shows."""
    state = hass.states.get(ENTITY)
    return state.attributes.get("media_title") if state else None


def available(hass: HomeAssistant) -> bool:
    """Return whether the entity is currently available."""
    state = hass.states.get(ENTITY)
    return state is not None and state.state != "unavailable"


async def test_push_updates_entity(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A change reported through the long poll reaches the entity."""
    await setup_player(hass, MASTER, MASTER_MAC)
    assert title(hass) == "PlexAmp"

    patch_session.player(MASTER).push("/Status", status_titled("Vinyl night"))

    await wait_for(lambda: title(hass) == "Vinyl night")


async def test_push_survives_malformed_xml(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """One garbled answer does not end the push loop for good."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)

    player.push("/Status", "<status><title1>cut off")
    player.push("/Status", status_titled("Still here"))

    await wait_for(lambda: title(hass) == "Still here")


async def test_push_survives_empty_status(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """An empty status document does not end the push loop for good."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)

    player.push("/Status", "<status/>")
    player.push("/Status", status_titled("Still here"))

    await wait_for(lambda: title(hass) == "Still here")


async def test_push_survives_http_error(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player error marks the entity unavailable until it answers again."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)

    player.push("/Status", "", status=503)
    await wait_for(lambda: not available(hass))

    player.push("/Status", status_titled("Back again"))
    await wait_for(lambda: available(hass) and title(hass) == "Back again")


async def test_push_survives_connection_loss(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player that drops off the network is picked up again when it returns."""
    await setup_player(hass, MASTER, MASTER_MAC)
    player = patch_session.player(MASTER)

    # The long poll in flight fails, and so does every retry while offline.
    player.offline = True
    player.push("/Status", "", status=503)
    await wait_for(lambda: not available(hass))

    player.offline = False
    player.push("/Status", status_titled("Reconnected"))
    await wait_for(lambda: available(hass) and title(hass) == "Reconnected")


async def test_slave_volume_is_its_own(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A slave shows its own volume, not the group's, and follows changes."""
    await setup_player(hass, SLAVE_1, SLAVE_1_MAC)
    entity = "media_player.player_2"

    def volume() -> float | None:
        state = hass.states.get(entity)
        return state.attributes.get("volume_level") if state else None

    await wait_for(lambda: volume() == 0.31)

    # Its own volume changes, from the BluOS app say: /SyncStatus reports it.
    changed = re.sub(
        r'(<SyncStatus\b[^>]*?)\bvolume="\d+"',
        r'\1volume="40"',
        fixture_text(LABEL, SLAVE_1, "sync_status"),
    ).replace('etag="108"', 'etag="109"')
    patch_session.player(SLAVE_1).push("/SyncStatus", changed)
    await wait_for(lambda: volume() == 0.40)
