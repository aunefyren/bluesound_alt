"""Every captured player: the media browser and sources follow what it offers.

Players differ in their inputs, presets and music services, by model, region
and account. Nothing about them is built in: each player's own /Browse and
/Presets decide what the media browser shows, and its inputs what the source
list holds. This runs over every capture with a browse menu.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from homeassistant.components.media_player import BrowseMedia
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent
import pytest

from custom_components.bluesound_alt.coordinator import _parse_browse
from custom_components.bluesound_alt.media_player import BluesoundMediaPlayer

from .conftest import FIXTURES, FakeBluOS, fixture_text, setup_player

CAPTURES = sorted(
    (path.parent.parent.name, path.parent.name)
    for path in FIXTURES.glob("*/*/browse_root.xml")
)


def player_of(directory: str) -> tuple[str, int]:
    """Return the address a capture directory is named after."""
    host, port = directory.rsplit("_", 1)
    return host, int(port)


def mac_of(label: str, player: tuple[str, int]) -> str:
    """Return a captured player's MAC, as the integration keys it."""
    sync = fixture_text(label, player, "sync_status")
    return re.search(r'mac="([^"]+)"', sync).group(1).replace(":", "").lower()


def captured_presets(label: str, player: tuple[str, int]) -> list[str]:
    """Return the names of a player's captured presets."""
    if not (FIXTURES / label / f"{player[0]}_{player[1]}" / "presets.xml").exists():
        return []
    return re.findall(
        r'<preset [^>]*name="([^"]*)"', fixture_text(label, player, "presets")
    )


@pytest.mark.parametrize(
    ("bluos", "directory"),
    CAPTURES,
    indirect=["bluos"],
    ids=[f"{label}-{directory}" for label, directory in CAPTURES],
)
async def test_capture_is_browsed_as_offered(
    hass: HomeAssistant, patch_session: FakeBluOS, directory: str
) -> None:
    """The browser lists the player's presets and menu; sources are its inputs."""
    label = patch_session.label
    player = player_of(directory)
    await setup_player(hass, player, mac_of(label, player))
    component: EntityComponent = hass.data["entity_components"]["media_player"]
    [entity] = component.entities
    assert isinstance(entity, BluesoundMediaPlayer)

    items, _ = _parse_browse(fixture_text(label, player, "browse_root"))
    presets = captured_presets(label, player)
    expected = (["Presets"] if presets else []) + [item["name"] for item in items]

    root = await entity.async_browse_media()
    children: list[BrowseMedia] = root.children or []
    assert [child.title for child in children[: len(expected)]] == expected
    # Every entry leads somewhere: played, opened, or both.
    for child in children[: len(expected)]:
        assert child.can_play or child.can_expand, child.title

    if presets:
        folder = children[0]
        listed = await entity.async_browse_media(
            folder.media_content_type, folder.media_content_id
        )
        assert [p.title for p in listed.children] == presets
        assert all(p.can_play for p in listed.children)

    inputs = [
        item["name"]
        for item in items
        if item["play_url"]
        and unquote(item["play_url"]).startswith("/Play?url=Capture:")
    ]
    receiver = [i["name"] for i in entity.coordinator.external_inputs]
    assert (entity.source_list or []) == inputs + receiver
