"""Regroup players without nesting groups or losing requests silently.

How BluOS players regroup, as observed on real players (BluOS 4.16.22; the
captures are in tests/fixtures/grouping*) and documented in the BluOS Custom
Integration API v1.7:

- /AddSlave sent to a player that follows another is ignored: it answers an
  empty <addSlave/>. Only a group's leader can add.
- Adding a player that follows another group moves it.
- Adding a player that leads its own group nests that group underneath, and
  the nested player stops. Such a player has to leave its group first.
- A leader asking to remove itself hands its group to one of the others. That
  moves playback along, so a leader playing an input answers
  <error>Cannot move input source</error>; its group can only be dissolved.
- A player asking to remove itself without leading anyone is refused; a
  follower leaves by asking its leader.
- Changes show up on the players within a second.

Joining follows Home Assistant's group dialog, which sends the whole selection:
members already in the group stay put, joining from a follower's card adds to
that group's leader, and a selected player from another group moves over.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import xmltodict

from .const import DOMAIN, GROUPING_POLL_SECONDS, GROUPING_WAIT_SECONDS
from .coordinator import (
    BluesoundCommandError,
    BluesoundUnreachableError,
    _fetch_sync_info,
    async_send_command,
)

type Address = tuple[str, int]

# A leader chain longer than this is a loop or a broken player, not a group.
MAX_LEADER_CHAIN = 4
# Rounds of breaking up groups before adding; each round deals with leaders a
# previous round created (a hand-over makes a new one).
MAX_BREAK_UP_ROUNDS = 3


@dataclass(frozen=True)
class PlayerState:
    """Where one player stands, read fresh from its /SyncStatus."""

    address: Address
    name: str
    master: Address | None
    slaves: tuple[Address, ...]
    fixed_secondary: bool = False


class BreakUp(Enum):
    """How to take apart a group whose leader has to go elsewhere."""

    # The leader leaves and the others play on under a new leader.
    HAND_OVER = "hand_over"
    # Everyone leaves, for when the others are coming along anyway, or the
    # playback cannot move.
    DISSOLVE = "dissolve"


# -- deciding ----------------------------------------------------------------


def break_up_plan(
    members: Sequence[PlayerState], leader: Address, requested: set[Address]
) -> list[tuple[PlayerState, BreakUp]]:
    """Choose which requested members must leave their own group, and how.

    Only leaders need to: a follower is moved simply by being added. A group
    whose other players are all being brought along is dissolved rather than
    handed over, since they would only be taken apart again.
    """
    plan = []
    for member in members:
        if member.address == leader or not member.slaves:
            continue
        coming_along = set(member.slaves) <= requested | {leader}
        plan.append((member, BreakUp.DISSOLVE if coming_along else BreakUp.HAND_OVER))
    return plan


def to_add(members: Iterable[PlayerState], leader: Address) -> list[Address]:
    """Return the requested members that are not already following the leader."""
    adding: list[Address] = []
    for member in members:
        if member.address == leader or member.master == leader:
            continue
        if member.address not in adding:
            adding.append(member.address)
    return adding


def added_players(answer: str) -> set[Address]:
    """Read which players an /AddSlave answer says were added."""
    parsed = xmltodict.parse(answer)
    slaves = (parsed.get("addSlave") or {}).get("slave") or []
    if isinstance(slaves, dict):
        slaves = [slaves]
    return {
        (slave.get("@id", ""), int(slave.get("@port") or 11000))
        for slave in slaves
        if isinstance(slave, dict)
    }


def slave_params(players: Sequence[Address]) -> dict[str, Any]:
    """Name one or several players in /AddSlave or /RemoveSlave."""
    if len(players) == 1:
        return {"slave": players[0][0], "port": players[0][1]}
    return {
        "slaves": ",".join(host for host, _ in players),
        "ports": ",".join(str(port) for _, port in players),
    }


# -- doing -------------------------------------------------------------------


async def async_join(
    session: aiohttp.ClientSession, target: Address, members: Sequence[Address]
) -> set[Address]:
    """Group members with target, as Home Assistant's join action asks.

    Returns every player whose grouping may have changed.
    """
    target_state = await _read(session, target)
    leader_state = await _leader_of(session, target_state)
    leader = leader_state.address

    states = [await _read(session, m) for m in dict.fromkeys(members) if m != target]
    for state in (target_state, leader_state, *states):
        if state.fixed_secondary:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="fixed_group",
                translation_placeholders={"name": state.name},
            )

    requested = {state.address for state in states}
    touched = {target, leader, *requested}

    for round_number in range(MAX_BREAK_UP_ROUNDS):
        plan = break_up_plan(states, leader, requested)
        if not plan:
            break
        for state, how in plan:
            touched.update(state.slaves)
            # Later rounds only meet leaders a hand-over made; dissolve those.
            await _break_up(
                session, state, how if round_number == 0 else BreakUp.DISSOLVE
            )
        states = [await _read(session, state.address) for state in states]
    if break_up_plan(states, leader, requested):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="not_regrouped"
        )

    adding = to_add(states, leader)
    if adding:
        answer = await async_send_command(
            session, _url(leader, "/AddSlave"), slave_params(adding)
        )
        missing = [
            address for address in adding if address not in added_players(answer)
        ]
        if missing:
            names = {state.address: state.name for state in states}
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_added",
                translation_placeholders={
                    "leader": leader_state.name,
                    "names": ", ".join(names.get(a, a[0]) for a in missing),
                },
            )
    return touched


async def async_unjoin(session: aiohttp.ClientSession, player: Address) -> set[Address]:
    """Take a player out of its group.

    Returns every player whose grouping may have changed.
    """
    state = await _read(session, player)
    if state.fixed_secondary:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="fixed_group",
            translation_placeholders={"name": state.name},
        )
    if state.master:
        # Only the leader can let a follower go, whether or not Home Assistant
        # knows the leader; its address comes from the follower's /SyncStatus.
        await async_send_command(
            session, _url(state.master, "/RemoveSlave"), slave_params([player])
        )
        return {player, state.master}
    if state.slaves:
        await _break_up(session, state, BreakUp.HAND_OVER)
        return {player, *state.slaves}
    return {player}


async def _break_up(
    session: aiohttp.ClientSession, leader: PlayerState, how: BreakUp
) -> None:
    """Take a group apart so its leader is on its own."""
    if how is BreakUp.HAND_OVER:
        try:
            await async_send_command(
                session,
                _url(leader.address, "/RemoveSlave"),
                slave_params([leader.address]),
            )
        except BluesoundCommandError:
            # Typically "Cannot move input source": the others cannot carry on
            # without this player, so they are let go instead.
            how = BreakUp.DISSOLVE
    if how is BreakUp.DISSOLVE:
        await async_send_command(
            session, _url(leader.address, "/RemoveSlave"), slave_params(leader.slaves)
        )
    await _wait_until_alone(session, leader.address)


async def _wait_until_alone(session: aiohttp.ClientSession, address: Address) -> None:
    """Wait for a player to report that it no longer leads anyone.

    Adding it before then would nest whatever it still leads.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GROUPING_WAIT_SECONDS
    while True:
        state = await _read(session, address)
        if not state.slaves:
            return
        if loop.time() >= deadline:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_regrouped"
            )
        await asyncio.sleep(GROUPING_POLL_SECONDS)


async def _leader_of(session: aiohttp.ClientSession, state: PlayerState) -> PlayerState:
    """Follow a player up to the leader of its group."""
    seen = {state.address}
    for _ in range(MAX_LEADER_CHAIN):
        if state.master is None or state.master in seen:
            return state
        seen.add(state.master)
        state = await _read(session, state.master)
    return state


async def _read(session: aiohttp.ClientSession, address: Address) -> PlayerState:
    """Read a player's grouping from its /SyncStatus."""
    info = await _fetch_sync_info(session, *address)
    if info is None:
        raise BluesoundUnreachableError(address[0], "no valid /SyncStatus")
    return PlayerState(
        address=address,
        name=info.name,
        master=(info.master_ip, info.master_port) if info.master_ip else None,
        slaves=tuple((slave["ip"], slave["port"]) for slave in info.slaves),
        fixed_secondary=info.fixed_secondary,
    )


def _url(address: Address, path: str) -> str:
    return f"http://{address[0]}:{address[1]}{path}"
