"""Tests for the player's options: hiding sources."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bluesound_alt.const import CONF_HIDDEN_SOURCES

from .conftest import FakeBluOS, config_entry_for, setup_player, wait_for
from .const import MASTER, MASTER_MAC

SPOTIFY = "browse:Spotify:play"
PRESET = "preset:1"


def source_list(hass: HomeAssistant) -> list[str]:
    """Return the master's source list."""
    return hass.states.get("media_player.player_1").attributes["source_list"]


async def hide(hass: HomeAssistant, entry_id: str, keys: list[str]) -> dict:
    """Open the options and submit the sources to hide."""
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HIDDEN_SOURCES: keys}
    )


async def test_options_offer_every_source(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The choices are the player's sources, by a key that outlives renames."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    selector = result["data_schema"].schema[CONF_HIDDEN_SOURCES].config
    assert [(o["value"], o["label"]) for o in selector["options"]] == [
        ("browse:Capture:hw:imxspdif,0/1/25/2?id=input2", "PlexAmp"),
        ("browse:Capture:plughw:imxnadadc,0/44100/24/2?id=input0", "Vinyl"),
        (SPOTIFY, "Spotify"),
        (PRESET, "P4 Lyden av Norge"),
    ]


async def test_hidden_sources_leave_the_list(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Hiding takes effect straight away, and unhiding brings them back."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)

    result = await hide(hass, entry.entry_id, [SPOTIFY, PRESET])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert source_list(hass) == ["PlexAmp", "Vinyl"]

    await hide(hass, entry.entry_id, [])
    await hass.async_block_till_done()
    assert source_list(hass) == ["PlexAmp", "Vinyl", "Spotify", "P4 Lyden av Norge"]


async def test_hidden_source_still_shows_while_playing(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Hiding only leaves a source out of the choices, not out of the state."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)

    await hide(hass, entry.entry_id, ["browse:Capture:hw:imxspdif,0/1/25/2?id=input2"])
    await hass.async_block_till_done()

    state = hass.states.get("media_player.player_1")
    assert "PlexAmp" not in state.attributes["source_list"]
    assert state.attributes["source"] == "PlexAmp"


async def test_hidden_source_stays_hidden_when_renamed(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A source renamed in the BluOS app stays hidden under its new name."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)
    await hide(
        hass, entry.entry_id, ["browse:Capture:plughw:imxnadadc,0/44100/24/2?id=input0"]
    )
    await hass.async_block_till_done()

    player = patch_session.player(MASTER)
    browse = player.captured("browse_root").replace('text="Vinyl"', 'text="Records"')
    player.overrides[("/Browse", frozenset())] = (200, browse)
    player.push(
        "/Status", player.captured("status").replace("<sid>4</sid>", "<sid>5</sid>")
    )
    await wait_for(
        lambda: any(s["name"] == "Records" for s in entry.runtime_data.sources)
    )
    await hass.async_block_till_done()

    assert source_list(hass) == ["PlexAmp", "Spotify", "P4 Lyden av Norge"]


async def test_hidden_source_gone_from_player_is_remembered(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Saving the options keeps hidden sources the player does not list now."""
    entry = await setup_player(hass, MASTER, MASTER_MAC)
    hass.config_entries.async_update_entry(
        entry, options={CONF_HIDDEN_SOURCES: ["preset:99"]}
    )

    await hide(hass, entry.entry_id, [SPOTIFY])

    assert entry.options[CONF_HIDDEN_SOURCES] == [SPOTIFY, "preset:99"]


async def test_options_need_the_player_set_up(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Without the player there is nothing to choose from."""
    entry = config_entry_for(MASTER, MASTER_MAC)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
