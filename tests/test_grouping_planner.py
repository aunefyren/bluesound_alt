"""Tests for the grouping decisions, which need no players at all."""

from __future__ import annotations

import pytest

from custom_components.bluesound_alt.grouping import (
    BreakUp,
    PlayerState,
    added_players,
    break_up_plan,
    slave_params,
    to_add,
)

LEADER = ("10.0.0.1", 11000)
A = ("10.0.0.2", 11000)
B = ("10.0.0.3", 11000)
C = ("10.0.0.4", 11000)
# Players like the NAD CI580 share one address across several ports.
C_SECOND_ZONE = ("10.0.0.4", 11010)


def player(address, master=None, slaves=()) -> PlayerState:
    """Build a player's state."""
    return PlayerState(address, f"player {address}", master, tuple(slaves))


def test_followers_need_no_break_up() -> None:
    """Only leaders have to leave their group first; followers just move."""
    members = [player(A, master=B), player(C)]

    assert break_up_plan(members, LEADER, {A, C}) == []


def test_leader_with_others_staying_behind_hands_over() -> None:
    """A leader whose followers are not coming along hands its group over."""
    members = [player(A, slaves=[B, C])]

    assert break_up_plan(members, LEADER, {A}) == [(members[0], BreakUp.HAND_OVER)]


def test_leader_with_everyone_coming_along_dissolves() -> None:
    """A group moving over in full is dissolved, not handed over first."""
    members = [player(A, slaves=[B]), player(B, master=A)]

    assert break_up_plan(members, LEADER, {A, B}) == [(members[0], BreakUp.DISSOLVE)]


def test_the_target_leader_is_never_broken_up() -> None:
    """The leader being joined keeps its own group."""
    members = [player(LEADER, slaves=[A])]

    assert break_up_plan(members, LEADER, {LEADER}) == []


def test_to_add_skips_existing_followers_and_duplicates() -> None:
    """Players already following the leader are not added again."""
    members = [player(A, master=LEADER), player(B), player(B), player(LEADER)]

    assert to_add(members, LEADER) == [B]


def test_ports_tell_players_on_one_address_apart() -> None:
    """Two zones of one multi-zone player are different players."""
    members = [player(C, master=LEADER), player(C_SECOND_ZONE)]

    assert to_add(members, LEADER) == [C_SECOND_ZONE]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("<addSlave></addSlave>", set()),
        ('<addSlave><slave id="10.0.0.2" port="11000"></slave></addSlave>', {A}),
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n<addSlave><slave id="10.0.0.4"'
            ' port="11000"/><slave id="10.0.0.3" port="11000"/></addSlave>',
            {B, C},
        ),
    ],
)
def test_added_players(answer: str, expected: set) -> None:
    """The /AddSlave answer lists exactly the players that were taken."""
    assert added_players(answer) == expected


def test_slave_params() -> None:
    """One player uses the singular form, several the batched form."""
    assert slave_params([A]) == {"slave": "10.0.0.2", "port": 11000}
    assert slave_params([A, C_SECOND_ZONE]) == {
        "slaves": "10.0.0.2,10.0.0.4",
        "ports": "11000,11010",
    }
