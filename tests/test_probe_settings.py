"""Tests for dev/probe_settings.py, which must only ever read from a player."""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

import pytest

DEV = pathlib.Path(__file__).parent.parent / "dev"

# A small tree in the shape the app reads: menus link on by id, and settings
# name the URL and parameter that would change them.
TREE = {
    None: '<settings id="root"><menu id="audio" displayName="Audio"/>'
    '<menu id="player" displayName="Player"/></settings>',
    "audio": '<settings id="audio">'
    '<setting id="profile" name="profile" url="/audiomodes" '
    'displayName="Sound profile" value="music">'
    '<value name="movie" displayName="Movie"/><value name="music" displayName="Music"/>'
    "</setting></settings>",
    "player": '<settings id="player"><setting id="rename" name="name" '
    'url="/Name" displayName="Player name" value="Kari&apos;s Soundbar"/>'
    "</settings>",
}


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    """Import dev/probe_settings.py, which imports probe_api by plain name."""
    spec = importlib.util.spec_from_file_location(
        "probe_settings", DEV / "probe_settings.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_reader(sent: list[tuple[str, dict]]):
    """Answer /Settings from TREE, recording every request."""

    def read(name: str, path: str, params: dict) -> dict:
        sent.append((path, params))
        body = TREE.get(params.get("id"))
        return {
            "name": name,
            "path": path,
            "params": params,
            "status": 200 if body else 404,
            "body": body or "",
        }

    return read


def test_walk_only_reads_settings(probe: ModuleType) -> None:
    """Every menu is reached, and no setting's change URL is ever requested."""
    sent: list[tuple[str, dict]] = []
    probe.walk_settings(fake_reader(sent), "32")

    assert {path for path, _ in sent} == {"/Settings"}
    assert {"audio", "player"} <= {params.get("id") for _, params in sent}


def test_profile_is_listed_with_its_command(probe: ModuleType) -> None:
    """A sound profile shows up with its options and the command to set it."""
    menus = probe.walk_settings(fake_reader([]), "32")
    found = {i["id"]: i for i in probe.interesting(menus)}

    profile = found["profile"]
    assert profile["changeWith"] == 'POST /audiomodes {"profile": <value>}'
    assert [o["displayName"] for o in profile["options"]] == ["Movie", "Music"]
    assert "rename" not in found
    assert "movie" not in {i.get("name") for i in found.values()}


def test_web_page_forms(probe: ModuleType) -> None:
    """The reboot form's method and action are read, in either attribute order."""
    body = (
        '<form action="/reboot" method="post"><input type="hidden" name="yes" '
        'value="1"><button name="go">Reboot</button></form>'
        "<form method='GET' action=/diag></form><a href=\"/log\">Log</a>"
    )
    (page,) = probe.web_forms({"body": body})
    assert page["forms"] == [
        {"method": "post", "action": "/reboot", "fields": ["yes=1", "go="]},
        {"method": "GET", "action": "/diag", "fields": []},
    ]
    assert page["links"] == ["/log"]


def test_only_settings_pages_are_followed(probe: ModuleType) -> None:
    """Of the web page's links, only its settings pages are read."""
    links = ["/diag", "/upgrade", "/settings?page_id=player&id=power", "/settings"]
    assert probe.settings_links(links) == [{"page_id": "player", "id": "power"}]


def test_report_is_scrubbed(probe: ModuleType) -> None:
    """The player's name and address are gone from the shareable report."""
    sync = (
        '<SyncStatus name="Kari\'s Soundbar" mac="02:AA:BB:CC:DD:EE" '
        'modelName="Pulse Soundbar+"/>'
    )
    records = [
        {
            "name": "sync_status",
            "path": "/SyncStatus",
            "params": {},
            "status": 200,
            "body": sync,
        },
        {
            "name": "web_root",
            "path": "/",
            "params": {},
            "status": 200,
            "body": "<p>Player at 192.168.1.20</p>",
        },
    ]
    scrubbed, leaks = probe.scrub("192.168.1.20", records, {"model": "Pulse Soundbar+"})

    text = str(scrubbed)
    assert "Kari" not in text
    assert "192.168.1.20" not in text
    assert "Pulse Soundbar+" in text
    assert leaks == []
