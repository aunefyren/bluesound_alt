"""Tests for talking to players: requests, parsing and failure handling."""

from __future__ import annotations

from urllib.parse import unquote

from homeassistant.core import HomeAssistant
import pytest

from custom_components.bluesound_alt.const import MAX_BROWSE_PAGES
from custom_components.bluesound_alt.coordinator import (
    BluesoundCoordinator,
    _fetch_presets,
    _fetch_sources,
    _fetch_sync_info,
    _parse_browse_items,
)

from .conftest import FakeBluOS, config_entry_for, fixture_text
from .const import LABEL, MASTER, MASTER_MAC, PAGING_LABEL, SLAVE_1, SLAVE_2, SOUNDBAR

# -- /SyncStatus ------------------------------------------------------------


async def test_sync_info_master(bluos: FakeBluOS) -> None:
    """A master lists its slaves."""
    info = await _fetch_sync_info(bluos, *MASTER)

    assert info is not None
    assert info.name == "Player 1"
    assert info.mac == MASTER_MAC
    assert info.model == "C700"
    assert (info.ip, info.port) == MASTER
    assert info.master_ip is None
    assert info.slaves == [
        {"ip": SLAVE_1[0], "port": 11000, "name": "Player 2"},
        {"ip": SLAVE_2[0], "port": 11000, "name": "Player 3"},
    ]


async def test_sync_info_slave(bluos: FakeBluOS) -> None:
    """A slave names its master and has no slaves of its own."""
    info = await _fetch_sync_info(bluos, *SLAVE_1)

    assert info is not None
    assert info.model == "PULSE FLEX 2i"
    assert info.master_ip == MASTER[0]
    assert info.slaves == []


async def test_sync_info_standalone(bluos: FakeBluOS) -> None:
    """A standalone player has neither master nor slaves."""
    info = await _fetch_sync_info(bluos, *SOUNDBAR)

    assert info is not None
    assert info.model == "PULSE SOUNDBAR+"
    assert info.master_ip is None
    assert info.slaves == []


async def test_sync_info_offline(bluos: FakeBluOS) -> None:
    """An unreachable player gives None rather than raising."""
    bluos.player(MASTER).offline = True

    assert await _fetch_sync_info(bluos, *MASTER) is None


async def test_sync_info_http_error(bluos: FakeBluOS) -> None:
    """A non-200 answer gives None."""
    bluos.player(MASTER).overrides[("/SyncStatus", frozenset())] = (503, "")

    assert await _fetch_sync_info(bluos, *MASTER) is None


async def test_sync_info_malformed(bluos: FakeBluOS) -> None:
    """Broken XML gives None rather than raising."""
    bluos.player(MASTER).overrides[("/SyncStatus", frozenset())] = (200, "<Sync")

    assert await _fetch_sync_info(bluos, *MASTER) is None


# -- /Browse sources and /Presets -------------------------------------------


async def test_sources_are_root_audio_items(bluos: FakeBluOS) -> None:
    """Sources are the playable top-level items, with their play URL decoded."""
    sources = await _fetch_sources(bluos, *MASTER)

    assert sources == [
        {"name": "PlexAmp", "play_url": "Capture:hw:imxspdif,0/1/25/2?id=input2"},
        {
            "name": "Vinyl",
            "play_url": "Capture:plughw:imxnadadc,0/44100/24/2?id=input0",
        },
        {"name": "Spotify", "play_url": "Spotify:play"},
    ]


async def test_source_matches_what_the_player_reports(bluos: FakeBluOS) -> None:
    """A source's play URL is what /Status reports while it plays.

    This is how the entity works out the current source, so the two
    spellings have to agree.
    """
    for player in (MASTER, SOUNDBAR):
        sources = await _fetch_sources(bluos, *player)
        status = fixture_text(LABEL, player, "status")
        stream_urls = {s["play_url"] for s in sources}
        assert any(f"<streamUrl>{url}</streamUrl>" in status for url in stream_urls)


async def test_sources_slave_without_inputs(bluos: FakeBluOS) -> None:
    """A speaker with no inputs of its own has no sources."""
    assert await _fetch_sources(bluos, *SLAVE_1) == []


async def test_sources_offline(bluos: FakeBluOS) -> None:
    """An unreachable player has no sources rather than an error."""
    bluos.player(MASTER).offline = True

    assert await _fetch_sources(bluos, *MASTER) == []


async def test_presets(bluos: FakeBluOS) -> None:
    """Presets carry id, name and image."""
    assert await _fetch_presets(bluos, *SOUNDBAR) == [
        {"id": "1", "name": "Soundbar", "image": "/images/capture/ic_tv.png"}
    ]
    assert await _fetch_presets(bluos, *SLAVE_1) == []


async def test_presets_malformed(bluos: FakeBluOS) -> None:
    """Broken XML gives no presets rather than raising."""
    bluos.player(MASTER).overrides[("/Presets", frozenset())] = (200, "<presets")

    assert await _fetch_presets(bluos, *MASTER) == []


# -- coordinator requests ---------------------------------------------------


async def coordinator_for(
    hass: HomeAssistant, bluos: FakeBluOS, player: tuple[str, int]
) -> BluesoundCoordinator:
    """Build a coordinator for a captured player, without starting it."""
    info = await _fetch_sync_info(bluos, *player)
    assert info is not None
    entry = config_entry_for(player, info.mac)
    return BluesoundCoordinator(hass, entry, player[0], player[1], info)


async def test_browse_walks_captured_tree(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Every browse key the player handed out leads to its captured menu.

    The fake session encodes the key into the URL as aiohttp does and decodes
    it again, so a key that is mangled on the way (double-encoded, or with
    its ampersands split into separate parameters) finds nothing.
    """
    coordinator = await coordinator_for(hass, patch_session, MASTER)

    root = await coordinator.async_browse(None)
    tunein = next(i for i in root if i["name"] == "TuneIn")
    menu = await coordinator.async_browse(tunein["browse_key"])
    assert [i["name"] for i in menu][:2] == ["For You", "Favorites"]

    radio = next(i for i in root if i["name"] == "Radio")
    for item in await coordinator.async_browse(radio["browse_key"]):
        captured = [
            e
            for e in patch_session.player(MASTER).responses
            if e["params"].get("key") == item["browse_key"]
        ]
        if captured:
            assert await coordinator.async_browse(item["browse_key"])


def page(title: str, next_key: str | None = None) -> tuple[int, str]:
    """Return a one-item browse page, pointing on to another if given."""
    next_attr = f' nextKey="{next_key}"' if next_key else ""
    return (
        200,
        f'<browse{next_attr}><item text="{title}" type="audio" '
        f'playURL="/Play?url={title}"/></browse>',
    )


@pytest.mark.parametrize("bluos", [PAGING_LABEL], indirect=True)
async def test_browse_follows_real_pages(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Airable's station charts continue onto a second page.

    The second page's key is the first page's nextKey, percent-encoded twice
    over, so it only matches its captured response if it reaches the player
    exactly as the player gave it out.
    """
    coordinator = await coordinator_for(hass, patch_session, MASTER)
    first = _parse_browse_items(fixture_text(PAGING_LABEL, MASTER, "browse_d2_05"))
    second = _parse_browse_items(
        fixture_text(PAGING_LABEL, MASTER, "browse_d2_05_page2")
    )
    radio = await coordinator.async_browse("Airable:")
    charts = next(i for i in radio if i["name"] == "Most popular stations")

    # Only two pages were captured; the request for the third finds nothing,
    # which ends the menu there.
    items = await coordinator.async_browse(charts["browse_key"])

    assert items == first + second
    assert len(items) == 40
    assert items[0]["name"] == "NRK P1+"
    assert items[20]["name"] == "NRK P1 Troms"


async def test_browse_follows_pages(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A menu split over pages is returned whole, in order."""
    coordinator = await coordinator_for(hass, patch_session, MASTER)
    overrides = patch_session.player(MASTER).overrides
    # Next keys carry their own query strings, escaped in the XML.
    overrides[("/Browse", frozenset({("key", "List:")}))] = page(
        "One", "List:?p=2&amp;n=20"
    )
    overrides[("/Browse", frozenset({("key", "List:?p=2&n=20")}))] = page(
        "Two", "List:?p=3&amp;n=20"
    )
    overrides[("/Browse", frozenset({("key", "List:?p=3&n=20")}))] = page("Three")

    items = await coordinator.async_browse("List:")

    assert [i["name"] for i in items] == ["One", "Two", "Three"]


async def test_browse_page_limit(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A menu that never ends stops at the page limit."""
    coordinator = await coordinator_for(hass, patch_session, MASTER)
    patch_session.player(MASTER).overrides[
        ("/Browse", frozenset({("key", "Loop:")}))
    ] = page("Again", "Loop:")

    items = await coordinator.async_browse("Loop:")

    assert len(items) == MAX_BROWSE_PAGES


async def test_browse_keeps_pages_before_a_failure(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """If a later page fails, the pages already fetched are still shown."""
    coordinator = await coordinator_for(hass, patch_session, MASTER)
    patch_session.player(MASTER).overrides[
        ("/Browse", frozenset({("key", "Partial:")}))
    ] = page("First", "Partial:gone")

    items = await coordinator.async_browse("Partial:")

    assert [i["name"] for i in items] == ["First"]


async def test_browse_failure_gives_empty_menu(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Unknown keys and unreachable players give an empty menu."""
    coordinator = await coordinator_for(hass, patch_session, MASTER)

    assert await coordinator.async_browse("Nope:") == []
    patch_session.player(MASTER).offline = True
    assert await coordinator.async_browse(None) == []


async def test_play_path_sent_verbatim(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A play URL from the player goes back byte for byte, not re-encoded."""
    coordinator = await coordinator_for(hass, patch_session, MASTER)
    # NRK P1+: percent-encoded colons, slashes and a "+" in the title.
    items = _parse_browse_items(fixture_text(LABEL, MASTER, "browse_d2_05"))
    play_url = items[0]["play_url"]
    assert play_url is not None
    assert "%2B" in play_url

    await coordinator.async_play_path(play_url)

    sent = patch_session.requests[-1]
    assert sent.raw_url == f"http://{MASTER[0]}:{MASTER[1]}{play_url}"
    assert sent.query["url"] == unquote(play_url.split("url=", 1)[1].split("&")[0])


async def test_individual_volume(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A player's own volume comes from /SyncStatus, grouped or not."""
    coordinator = await coordinator_for(hass, patch_session, SLAVE_1)

    assert coordinator.individual_volume == 31
    info = await _fetch_sync_info(patch_session, *SOUNDBAR)
    assert info is not None
    assert info.volume == 61
