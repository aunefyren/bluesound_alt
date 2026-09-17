"""Check the fake players' grouping model against real players.

The grouping tests are only as good as the model behind them, so both captured
grouping runs are replayed through it, step by step, and after each step every
player has to end up where the real one did: following the same leader,
leading the same players, and playing or stopped alike.
"""

from __future__ import annotations

import json
import re

import pytest

from .conftest import FIXTURES, FakeBluOS, Request


def settled(step: dict) -> dict[tuple[str, int], dict]:
    """Each player's last reported /SyncStatus in a step, reduced to topology."""
    last: dict[tuple[str, int], str] = {}
    for record in step["timeline"]:
        last[(record["host"], record["port"])] = record["body"]
    return {address: topology(body) for address, body in last.items()}


def topology(body: str) -> dict:
    """Who a /SyncStatus body says a player follows and leads."""
    master = re.search(r'<master port="(\d+)">([^<]+)</master>', body)
    return {
        "master": (master.group(2), int(master.group(1))) if master else None,
        "slaves": sorted(
            (ip, int(port))
            for ip, port in re.findall(r'<slave id="([^"]+)" port="(\d+)"', body)
        ),
    }


def stopped(body: str) -> bool:
    """Whether a /Status body says nothing is playing."""
    return "<state>stop</state>" in body or "<state>" not in body


def replay(label: str, upto: int) -> tuple[dict, str, FakeBluOS]:
    """Run the captured steps through the model, up to and including one.

    Returns that step, the model's answer to it, and the fake players.
    """
    bluos = FakeBluOS(label)
    steps = json.loads((FIXTURES / label / "steps.json").read_text("utf-8"))["steps"]
    body = ""
    for step in steps[: upto + 1]:
        action = step["action"]
        player = bluos.players[(action["host"], action["port"])]
        response = player.answer(
            Request(
                host=action["host"],
                port=action["port"],
                path=action["path"],
                query={k: str(v) for k, v in action["params"].items()},
                raw_url="",
            )
        )
        body = response._body
    return steps[upto], body, bluos


def cases(label: str) -> list:
    """One test case per captured step."""
    steps = json.loads((FIXTURES / label / "steps.json").read_text("utf-8"))["steps"]
    return [
        pytest.param(label, index, id=f"{label}-{step['name']}")
        for index, step in enumerate(steps)
    ]


@pytest.mark.parametrize(
    ("label", "index"), [*cases("grouping"), *cases("grouping-leader-leaves")]
)
def test_model_matches_real_players(label: str, index: int) -> None:
    """After each captured step, the model agrees with the real players."""
    step, response, bluos = replay(label, index)
    name, model = step["name"], bluos.grouping

    for address, real in settled(step).items():
        assert {
            "master": model.leader_of(address),
            "slaves": sorted(model.slaves_of(address)),
        } == real, f"{name}: topology of {address}"

    for record in step["status_after"]:
        address = (record["host"], record["port"])
        assert stopped(model.status(address)) == stopped(record["body"]), (
            f"{name}: playback of {address}"
        )

    real_error = "<error>" in step["action"].get("body", "")
    assert ("<error>" in response) == real_error, f"{name}: error response"
    if step["action"]["path"] == "/AddSlave":
        real_added = re.findall(r'<slave id="([^"]+)"', step["action"]["body"])
        added = re.findall(r'<slave id="([^"]+)"', response)
        assert sorted(added) == sorted(real_added), f"{name}: players reported as added"
