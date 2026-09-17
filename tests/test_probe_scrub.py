"""Tests for the probe's scrubbing, which decides what users can safely share.

Scrubbed dumps get attached to issues and turned into committed fixtures, so
each case here is an identifier that must not survive.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

DEV = pathlib.Path(__file__).parent.parent / "dev"


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    """Import dev/probe_api.py, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location("probe_api", DEV / "probe_api.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_api"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scrubber(probe: ModuleType):
    """Return a scrubber that knows one player, as after harvesting a dump."""
    scrubber = probe.Scrubber()
    scrubber.learn_host("192.168.1.20")
    scrubber.learn_name("Kari's Office")
    scrubber.learn_group("Kari's Office + 2")
    scrubber.learn_mac("02:AA:BB:CC:DD:EE")
    return scrubber


def test_player_identity(scrubber) -> None:
    """Addresses, MAC and name in /SyncStatus become stable fakes."""
    body = scrubber.scrub_xml(
        '<SyncStatus name="Kari&apos;s Office" modelName="NODE" '
        'id="192.168.1.20:11000" mac="02:AA:BB:CC:DD:EE" group="Kari&apos;s '
        'Office + 2"><master port="11000">192.168.1.21</master></SyncStatus>'
    )

    assert 'name="Player 1"' in body
    assert 'group="Player 1 + 2"' in body
    assert 'id="192.0.2.1:11000"' in body
    assert 'mac="00:00:5E:00:53:01"' in body
    assert ">192.0.2.2</master>" in body
    assert 'modelName="NODE"' in body
    assert scrubber.leaks([body]) == []


def test_serial_nested_in_encoded_url(scrubber) -> None:
    """TuneIn's serial, a MAC percent-encoded twice inside a browse key."""
    key = (
        "https%253A%252F%252Fapi.radiotime.com%252Fcategories%252Fhome"
        "%253Fserial%253D02%25253A11%25253A22%25253A33%25253A44%25253A55"
        "%2526partnerId%253D8OeGua6y"
    )

    scrubbed = scrubber.scrub_text(key)

    assert "serial%253DREDACTED%2526partnerId%253D8OeGua6y" in scrubbed
    assert "11%25253A22" not in scrubbed
    assert scrubber.leaks([scrubbed]) == []


def test_unknown_encoded_mac_is_reported(scrubber) -> None:
    """A MAC nobody taught the scrubber still trips the leak check."""
    assert scrubber.leaks(["x%253D02%25253A11%25253A22%25253A33%25253A44%25253A55"])


@pytest.mark.parametrize(
    ("text", "gone", "kept"),
    [
        ("a?userId=12345&b=1", "12345", "&b=1"),
        ("mail someone@example.org here", "someone@example.org", "here"),
        ("https://5550001234.airable.io/radio", "5550001234", ".airable.io/radio"),
        ("http://192.168.1.50:8000/stream", "192.168.1.50", ":8000/stream"),
        ("serial=02aabbccddee", "02aabbccddee", "serial="),
    ],
)
def test_free_text(scrubber, text: str, gone: str, kept: str) -> None:
    """Account parameters, e-mails, Airable hosts and local addresses go."""
    scrubbed = scrubber.scrub_text(text)

    assert gone not in scrubbed
    assert kept in scrubbed


def test_public_addresses_are_kept(scrubber) -> None:
    """Radio stream servers on public addresses are not personal."""
    assert scrubber.scrub_text("http://8.8.8.8:8000/live") == (
        "http://8.8.8.8:8000/live"
    )


def test_encoding_is_preserved(scrubber) -> None:
    """Scrubbing leaves the escaping that encoding bugs depend on untouched."""
    body = (
        '<item playURL="/Play?url=Capture%3Ahw%3A1%2C0%2F1%2F25%2F2&amp;id=input1"'
        ' text="Optical"/>'
    )

    assert scrubber.scrub_xml(body) == body
