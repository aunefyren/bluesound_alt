"""Tests for restarting a player."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .conftest import FakeBluOS, setup_player
from .const import SOUNDBAR, SOUNDBAR_MAC


async def test_restart_posts_the_reboot_form(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Restart submits the web page's reboot confirmation, on port 80."""
    await setup_player(hass, SOUNDBAR, SOUNDBAR_MAC)

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.player_4_restart"}, blocking=True
    )

    sent = [r for r in patch_session.requests if r.path == "/reboot"]
    assert [(r.method, r.port, r.body) for r in sent] == [
        ("POST", 80, {"noheader": "1", "yes": ""})
    ]
