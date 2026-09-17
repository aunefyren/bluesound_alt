"""Tests for diagnostics, which get attached to public issues."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.bluesound_alt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import FakeBluOS, fixture_text, setup_player
from .const import (
    LABEL,
    MASTER,
    MASTER_MAC,
    SLAVE_1,
    SLAVE_1_MAC,
    SOUNDBAR,
    SOUNDBAR_MAC,
)

SERIAL = "02:AA:BB:CC:DD:EE"


async def diagnostics_for(
    hass: HomeAssistant, player: tuple[str, int], mac: str
) -> dict:
    """Set a player up and return its diagnostics."""
    entry = await setup_player(hass, player, mac)
    return await async_get_config_entry_diagnostics(hass, entry)


def identifiers(player: tuple[str, int], mac: str) -> list[str]:
    """Values that would identify the home if they leaked."""
    return [player[0], mac, mac.upper(), "Player 1", "Player 2", "Player 3"]


async def test_master_is_redacted(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Addresses, MACs and player names are all gone."""
    result = await diagnostics_for(hass, MASTER, MASTER_MAC)

    dumped = json.dumps(result)
    for value in [*identifiers(MASTER, MASTER_MAC), SLAVE_1[0]]:
        assert value not in dumped
    assert "00:00:5E:00:53" not in dumped


async def test_stream_url_account_ids_are_redacted(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """TuneIn puts the player's serial into its stream URL; it is removed."""
    status = fixture_text(LABEL, SOUNDBAR, "status").replace(
        "<streamUrl>Capture:hdmi-input?id=input4</streamUrl>",
        "<streamUrl>TuneIn:s24896/http://opml.radiotime.com/Tune.ashx?"
        f"id=s24896&amp;formats=mp3&amp;serial={SERIAL}&amp;partnerId=x"
        "</streamUrl>",
    )
    patch_session.player(SOUNDBAR).overrides[("/Status", frozenset())] = (
        200,
        status,
    )

    result = await diagnostics_for(hass, SOUNDBAR, SOUNDBAR_MAC)

    stream_url = result["status"]["stream_url"]
    assert SERIAL not in json.dumps(result)
    assert "serial=**REDACTED**" in stream_url
    # The rest of the URL is what makes it useful for debugging.
    assert stream_url.startswith("TuneIn:s24896/http://opml.radiotime.com/")
    assert "partnerId=x" in stream_url


async def test_useful_state_is_kept(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Redaction leaves what is needed to debug sources and playback."""
    result = await diagnostics_for(hass, MASTER, MASTER_MAC)

    assert result["device"]["model"] == "C700"
    assert result["group"]["role"] == "master"
    assert len(result["group"]["slaves"]) == 2
    assert result["status"]["title"] == "PlexAmp"
    assert result["status"]["stream_url"] == "Capture:hw:imxspdif,0/1/25/2?id=input2"
    assert [s["name"] for s in result["sources"]] == ["PlexAmp", "Vinyl", "Spotify"]
    assert result["presets"][0]["name"] == "P4 Lyden av Norge"
    assert result["last_update_success"] is True
    assert result["push_loop_running"] is True


async def test_slave(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A slave reports its role and own volume, with its master redacted."""
    result = await diagnostics_for(hass, SLAVE_1, SLAVE_1_MAC)

    assert result["group"]["role"] == "slave"
    assert result["group"]["master_ip"] == "**REDACTED**"
    assert result["individual_volume"] == 31
    assert result["sync_loop_running"] is True
    dumped = json.dumps(result)
    for value in [*identifiers(SLAVE_1, SLAVE_1_MAC), MASTER[0]]:
        assert value not in dumped
