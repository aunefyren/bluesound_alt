"""Fixtures for the Bluesound Alt tests.

Players are faked at the HTTP session boundary, answering from responses
captured on real hardware (see dev/probe_api.py and dev/make_fixtures.py).
Everything above the session -- URL building, XML parsing, error handling --
runs for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
import json
import pathlib
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.bluesound_alt.const import DOMAIN

from .const import LABEL

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let every test load the integration from custom_components."""
    return


def fixture_text(label: str, player: tuple[str, int], name: str) -> str:
    """Return one captured response body."""
    host, port = player
    return (FIXTURES / label / f"{host}_{port}" / f"{name}.xml").read_text(
        encoding="utf-8"
    )


class FakeResponse:
    """The slice of aiohttp.ClientResponse the integration uses."""

    def __init__(
        self,
        status: int = 200,
        body: str = "",
        *,
        pending: Awaitable[tuple[int, str]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Store what this response will return, now or once it arrives."""
        self.status = status
        self._body = body
        self._pending = pending
        self._error = error

    async def text(self) -> str:
        """Return the body."""
        return self._body

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context, which is where aiohttp raises."""
        if self._error is not None:
            raise self._error
        if self._pending is not None:
            self.status, self._body = await self._pending
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave the response context."""


@dataclass
class Request:
    """One request as the player would have received it."""

    host: str
    port: int
    path: str
    query: dict[str, str]
    raw_url: str


@dataclass
class FakePlayer:
    """A player that answers with its captured responses."""

    host: str
    port: int
    responses: list[dict[str, Any]]
    directory: pathlib.Path
    offline: bool = False
    timing_out: bool = False
    # (path, frozenset(query items)) -> (status, body), checked first.
    overrides: dict[tuple[str, frozenset], tuple[int, str]] = field(
        default_factory=dict
    )
    # Answers for long polls (requests carrying an etag), per path. A long
    # poll waits until the test pushes something, as a real player waits
    # until something changes.
    _long_polls: dict[str, asyncio.Queue[tuple[int, str]]] = field(default_factory=dict)

    def push(self, path: str, body: str, status: int = 200) -> None:
        """Answer the next long poll on a path, e.g. "/Status"."""
        self._long_poll_queue(path).put_nowait((status, body))

    def _long_poll_queue(self, path: str) -> asyncio.Queue[tuple[int, str]]:
        return self._long_polls.setdefault(path, asyncio.Queue())

    def answer(self, request: Request) -> FakeResponse:
        """Find the captured response matching a request."""
        if self.offline:
            return FakeResponse(
                error=aiohttp.ClientConnectionError(f"{self.host} is offline")
            )
        if self.timing_out:
            return FakeResponse(error=TimeoutError())

        query = frozenset(request.query.items())
        if (request.path, query) in self.overrides:
            return FakeResponse(*self.overrides[(request.path, query)])

        if "etag" in request.query:
            return FakeResponse(pending=self._long_poll_queue(request.path).get())

        for entry in self.responses:
            if entry["path"] == request.path and entry["params"] == request.query:
                return self._from_entry(entry)
        return FakeResponse(404, "")

    def _named(self, name: str) -> FakeResponse:
        entry = next(e for e in self.responses if e["name"] == name)
        return self._from_entry(entry)

    def _from_entry(self, entry: dict[str, Any]) -> FakeResponse:
        body = ""
        if "file" in entry:
            body = (self.directory / entry["file"]).read_text(encoding="utf-8")
        return FakeResponse(entry["status"] or 500, body)


class FakeBluOS:
    """A network of captured players, reachable through a fake session."""

    def __init__(self, label: str) -> None:
        """Load every player captured under a label."""
        self.label = label
        self.players: dict[tuple[str, int], FakePlayer] = {}
        self.requests: list[Request] = []
        for index_file in sorted((FIXTURES / label).glob("*/index.json")):
            index = json.loads(index_file.read_text(encoding="utf-8"))
            self.players[(index["host"], index["port"])] = FakePlayer(
                host=index["host"],
                port=index["port"],
                responses=index["requests"],
                directory=index_file.parent,
            )

    @property
    def closed(self) -> bool:
        """Sessions handed out by Home Assistant stay open."""
        return False

    def get(
        self, url: str | URL, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> FakeResponse:
        """Answer a GET the way the addressed player would.

        The URL is assembled as aiohttp assembles it and then decoded again,
        so a value that does not survive the round trip will not match its
        captured response.
        """
        full = URL(url) if isinstance(url, str) else url
        if params:
            full = full.extend_query({k: str(v) for k, v in params.items()})
        request = Request(
            host=full.host or "",
            port=full.port or 80,
            path=full.path,
            query=dict(full.query),
            raw_url=str(full),
        )
        self.requests.append(request)

        player = self.players.get((request.host, request.port))
        if player is None:
            return FakeResponse(
                error=aiohttp.ClientConnectionError(
                    f"no route to {request.host}:{request.port}"
                )
            )
        return player.answer(request)

    def player(self, address: tuple[str, int]) -> FakePlayer:
        """Return a player, to adjust how it behaves."""
        return self.players[address]


@pytest.fixture
def bluos(request: pytest.FixtureRequest) -> FakeBluOS:
    """Return captured players: the home network, or another capture.

    Pick another capture with
    @pytest.mark.parametrize("bluos", ["paging"], indirect=True).
    """
    return FakeBluOS(getattr(request, "param", LABEL))


@pytest.fixture
def patch_session(bluos: FakeBluOS) -> Iterator[FakeBluOS]:
    """Route every session the integration asks Home Assistant for to the fake."""
    with (
        patch(
            "custom_components.bluesound_alt.coordinator.async_get_clientsession",
            return_value=bluos,
        ),
        patch(
            "custom_components.bluesound_alt.async_get_clientsession",
            return_value=bluos,
        ),
        patch(
            "custom_components.bluesound_alt.config_flow.async_get_clientsession",
            return_value=bluos,
        ),
        # Retry back-off, which would otherwise stall tests for seconds.
        patch(
            "custom_components.bluesound_alt.coordinator.RETRY_DELAY", 0, create=True
        ),
    ):
        yield bluos


def config_entry_for(player: tuple[str, int], unique_id: str) -> MockConfigEntry:
    """Return a config entry for a captured player."""
    host, port = player
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={CONF_HOST: host, CONF_PORT: port},
    )


async def setup_player(
    hass: HomeAssistant, player: tuple[str, int], unique_id: str
) -> MockConfigEntry:
    """Add a captured player to Home Assistant and set it up."""
    entry = config_entry_for(player, unique_id)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    # Long-running loops must not count as setup work still in progress, or
    # Home Assistant's startup waits on them. Bounded, so that fails loudly.
    async with asyncio.timeout(5):
        await hass.async_block_till_done()
    return entry


async def wait_for(condition: Callable[[], bool], attempts: int = 200) -> None:
    """Let background tasks run until a condition holds."""
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")
