"""Grouping through Home Assistant's join and unjoin actions.

The players are the captured home: the C700 leads both Flex speakers, and the
soundbar is on its own. Regrouping is answered by the grouping model in
tests/fake_grouping.py, which replays what real players did (see
test_fake_grouping.py), so each test checks where the players really end up,
not which requests were sent.

The behaviour asked for:
- Joining sends the whole selection, as Home Assistant's own group dialog
  does, and members already in the group are left where they are.
- Joining from a follower's card adds to that group's leader rather than
  making the follower lead, which would change what the group plays.
- A selected player that is in another group moves; its old group carries on
  without it where its playback can move, and is dissolved where it cannot.
- Unjoining a leader hands its group to the others where possible.
- Groups are never nested, and refusals and errors are raised, not swallowed.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from .conftest import FakeBluOS, setup_player, wait_for
from .const import (
    MASTER,
    MASTER_MAC,
    SLAVE_1,
    SLAVE_1_MAC,
    SLAVE_2,
    SLAVE_2_MAC,
    SOUNDBAR,
    SOUNDBAR_MAC,
)

C700 = "media_player.player_1"
FLEX_1 = "media_player.player_2"
FLEX_2 = "media_player.player_3"
SOUNDBAR_ENTITY = "media_player.player_4"


async def setup_home(hass: HomeAssistant, *, without: tuple = ()) -> None:
    """Set up the four captured players, optionally leaving some out."""
    for player, mac in (
        (MASTER, MASTER_MAC),
        (SLAVE_1, SLAVE_1_MAC),
        (SLAVE_2, SLAVE_2_MAC),
        (SOUNDBAR, SOUNDBAR_MAC),
    ):
        if player not in without:
            await setup_player(hass, player, mac)


async def join(hass: HomeAssistant, target: str, members: list[str]) -> None:
    """Call media_player.join as Home Assistant's group dialog does."""
    await hass.services.async_call(
        "media_player",
        "join",
        {"entity_id": target, "group_members": members},
        blocking=True,
    )


async def unjoin(hass: HomeAssistant, target: str) -> None:
    """Call media_player.unjoin."""
    await hass.services.async_call(
        "media_player", "unjoin", {"entity_id": target}, blocking=True
    )


def group_members(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return the group members an entity currently shows."""
    state = hass.states.get(entity_id)
    return list(state.attributes.get("group_members") or []) if state else []


def assert_not_nested(bluos: FakeBluOS) -> None:
    """No player may both follow and lead."""
    assert bluos.grouping.nested() == []


# -- joining ----------------------------------------------------------------


async def test_add_speaker_from_the_leader(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The dialog on the leader adds the soundbar, sending existing members too."""
    await setup_home(hass)
    model = patch_session.grouping

    await join(hass, C700, [FLEX_1, FLEX_2, SOUNDBAR_ENTITY])

    assert sorted(model.slaves_of(MASTER)) == sorted([SLAVE_1, SLAVE_2, SOUNDBAR])
    assert_not_nested(patch_session)
    await wait_for(
        lambda: (
            sorted(group_members(hass, SOUNDBAR_ENTITY))
            == sorted([C700, FLEX_1, FLEX_2, SOUNDBAR_ENTITY])
        )
    )


async def test_add_speaker_from_a_follower(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The dialog on a follower adds to the group, which keeps its leader.

    Players ignore an add sent to a follower, so it goes to the leader.
    """
    await setup_home(hass)
    model = patch_session.grouping

    await join(hass, FLEX_1, [C700, FLEX_2, SOUNDBAR_ENTITY])

    assert sorted(model.slaves_of(MASTER)) == sorted([SLAVE_1, SLAVE_2, SOUNDBAR])
    assert model.leader_of(SLAVE_1) == MASTER
    assert_not_nested(patch_session)


async def test_move_follower_to_another_speaker(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A follower chosen by another speaker moves; its old group carries on."""
    await setup_home(hass)
    model = patch_session.grouping

    await join(hass, SOUNDBAR_ENTITY, [FLEX_2])

    assert model.slaves_of(SOUNDBAR) == [SLAVE_2]
    assert model.slaves_of(MASTER) == [SLAVE_1]
    assert_not_nested(patch_session)
    await wait_for(
        lambda: (
            group_members(hass, SOUNDBAR_ENTITY) == [SOUNDBAR_ENTITY, FLEX_2]
            and group_members(hass, C700) == [C700, FLEX_1]
        )
    )


async def test_move_leader_playing_an_input(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A leader playing an input moves on its own; its group is dissolved.

    The input cannot follow the rest of the group anywhere. Added as it is,
    the leader would nest its group underneath.
    """
    await setup_home(hass)
    model = patch_session.grouping

    await join(hass, SOUNDBAR_ENTITY, [C700])

    assert model.slaves_of(SOUNDBAR) == [MASTER]
    assert model.slaves_of(MASTER) == []
    assert model.leader_of(SLAVE_1) is None
    assert model.leader_of(SLAVE_2) is None
    assert_not_nested(patch_session)


async def test_move_leader_playing_a_stream(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A leader playing a stream moves on its own; its group plays on."""
    model = patch_session.grouping
    model.set_movable(MASTER)
    await setup_home(hass)

    await join(hass, SOUNDBAR_ENTITY, [C700])

    assert model.slaves_of(SOUNDBAR) == [MASTER]
    assert model.slaves_of(MASTER) == []
    # The last listed follower takes over the rest.
    assert model.slaves_of(SLAVE_2) == [SLAVE_1]
    assert_not_nested(patch_session)


async def test_merge_groups_by_selecting_everyone(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Selecting every player of another group brings them all over, flat."""
    model = patch_session.grouping
    model.arrange({MASTER: [SLAVE_1], SOUNDBAR: [SLAVE_2]})
    await setup_home(hass)

    await join(hass, SOUNDBAR_ENTITY, [FLEX_2, C700, FLEX_1])

    assert sorted(model.slaves_of(SOUNDBAR)) == sorted([SLAVE_1, SLAVE_2, MASTER])
    assert_not_nested(patch_session)


async def test_join_refuses_player_in_fixed_group(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A player in a stereo pair or similar is refused with an error."""
    model = patch_session.grouping
    model.set_fixed(SOUNDBAR)
    await setup_home(hass)

    with pytest.raises(HomeAssistantError):
        await join(hass, C700, [FLEX_1, FLEX_2, SOUNDBAR_ENTITY])

    assert model.leader_of(SOUNDBAR) is None


async def test_join_reports_players_not_added(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """When the player declines the add, the action fails instead of passing."""
    model = patch_session.grouping
    await setup_home(hass)
    model.refuse_adds = True

    with pytest.raises(HomeAssistantError):
        await join(hass, C700, [FLEX_1, FLEX_2, SOUNDBAR_ENTITY])


async def test_move_part_of_a_streaming_group(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Taking a leader and one follower leaves the other follower behind.

    The leader hands its group over, which makes the selected follower a new
    leader; that group is then let go, so both arrive flat.
    """
    model = patch_session.grouping
    model.set_movable(MASTER)
    await setup_home(hass)

    await join(hass, SOUNDBAR_ENTITY, [C700, FLEX_2])

    assert sorted(model.slaves_of(SOUNDBAR)) == sorted([MASTER, SLAVE_2])
    assert model.leader_of(SLAVE_1) is None
    assert_not_nested(patch_session)


async def test_join_fails_when_players_do_not_regroup(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A leader that never lets its group go is not added, which would nest."""
    model = patch_session.grouping
    await setup_home(hass)
    # The C700 accepts the request but keeps its followers.
    stuck = model.sync_status(MASTER)
    for query in (
        frozenset({("slave", MASTER[0]), ("port", "11000")}),
        frozenset({("slaves", f"{SLAVE_1[0]},{SLAVE_2[0]}"), ("ports", "11000,11000")}),
    ):
        patch_session.player(MASTER).overrides[("/RemoveSlave", query)] = (200, stuck)

    with (
        patch("custom_components.bluesound_alt.grouping.GROUPING_WAIT_SECONDS", 0),
        pytest.raises(HomeAssistantError),
    ):
        await join(hass, SOUNDBAR_ENTITY, [C700])

    assert model.leader_of(MASTER) is None
    assert_not_nested(patch_session)


async def test_join_error_names_the_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Refusals come with a readable message, not just a key."""
    model = patch_session.grouping
    model.set_fixed(SOUNDBAR)
    await setup_home(hass)

    with pytest.raises(ServiceValidationError) as err:
        await join(hass, C700, [FLEX_1, FLEX_2, SOUNDBAR_ENTITY])

    assert "Player 4" in str(err.value)
    assert "fixed group" in str(err.value)


# -- unjoining --------------------------------------------------------------


async def test_unjoin_follower(hass: HomeAssistant, patch_session: FakeBluOS) -> None:
    """A follower leaves; the rest of the group plays on."""
    await setup_home(hass)
    model = patch_session.grouping

    await unjoin(hass, FLEX_1)

    assert model.leader_of(SLAVE_1) is None
    assert model.slaves_of(MASTER) == [SLAVE_2]
    await wait_for(lambda: group_members(hass, C700) == [C700, FLEX_2])


async def test_unjoin_follower_whose_leader_is_not_set_up(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A follower can leave a leader Home Assistant does not know.

    Players refuse a follower's request to remove itself, so the removal goes
    to the leader, found through the follower's /SyncStatus.
    """
    await setup_home(hass, without=(MASTER,))
    model = patch_session.grouping

    await unjoin(hass, FLEX_1)

    assert model.leader_of(SLAVE_1) is None


async def test_unjoin_leader_playing_an_input(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A leader playing an input leaves; the group cannot carry on without it."""
    await setup_home(hass)
    model = patch_session.grouping

    await unjoin(hass, C700)

    assert model.slaves_of(MASTER) == []
    assert model.leader_of(SLAVE_1) is None
    assert model.leader_of(SLAVE_2) is None
    assert "<state>stop</state>" not in model.status(MASTER)


async def test_unjoin_leader_playing_a_stream(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """A leader playing a stream leaves; the others carry on as a group."""
    model = patch_session.grouping
    model.set_movable(MASTER)
    await setup_home(hass)

    await unjoin(hass, C700)

    assert model.slaves_of(MASTER) == []
    assert model.slaves_of(SLAVE_2) == [SLAVE_1]


async def test_unjoin_standalone_player(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Unjoining a player that is on its own changes nothing."""
    await setup_home(hass)
    model = patch_session.grouping

    await unjoin(hass, SOUNDBAR_ENTITY)

    assert model.leader_of(SOUNDBAR) is None
    assert model.slaves_of(MASTER) == [SLAVE_1, SLAVE_2]


async def test_unjoin_refuses_fixed_group_secondary(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """The hidden half of a stereo pair is not taken apart from its primary."""
    model = patch_session.grouping
    model.set_fixed(SLAVE_1)
    await setup_home(hass)

    with pytest.raises(ServiceValidationError):
        await unjoin(hass, FLEX_1)

    assert model.leader_of(SLAVE_1) == MASTER


# -- errors from commands ---------------------------------------------------


async def test_command_error_is_raised(
    hass: HomeAssistant, patch_session: FakeBluOS
) -> None:
    """Players report failures as HTTP 200 with an <error> body; that surfaces."""
    await setup_home(hass)
    patch_session.player(MASTER).overrides[("/Play", frozenset())] = (
        200,
        '<?xml version="1.0" encoding="UTF-8"?>\n<error>Cannot play</error>',
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "media_player", "media_play", {"entity_id": C700}, blocking=True
        )
