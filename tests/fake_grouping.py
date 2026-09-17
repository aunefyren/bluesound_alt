"""A grouping model for the fake players.

Captured responses cover how players look, but not how they regroup, so this
answers /AddSlave and /RemoveSlave from a model of who leads whom, and rewrites
/SyncStatus and /Status to match once anything has changed.

Every rule is taken from real players (tests/fixtures/grouping and
tests/fixtures/grouping-leader-leaves, BluOS 4.16.22) or, where marked, from
the BluOS Custom Integration API v1.7:

- /AddSlave sent to a player that follows another is refused: an empty
  <addSlave/> and no change.
- Adding a player that follows another group moves it.
- Adding a player that leads its own group nests that group underneath, and
  the nested player stops.
- /RemoveSlave of the leader, sent to the leader, hands the group to one of the
  others, the last listed. That moves playback with it, so a leader playing an
  input answers <error>Cannot move input source</error> and nothing changes;
  with nothing playing there is nothing to move, and it goes ahead.
- A player asking itself to leave is treated as a leader handing over its
  group; without followers of its own it answers
  <error>no slave available as new master</error> and nothing changes.
- A player that leaves a group stops.
- /Status of a player that follows is its leader's (documented).
- A leader reports a new follower in steps: its syncStat moves on before its
  /SyncStatus lists everyone (see add_batched in tests/fixtures/grouping).
  With staged_adds set, the last player added is left out of the leader's
  /SyncStatus for one more read, and only /SyncStatus announces the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import FakePlayer

Address = tuple[str, int]

_MASTER = re.compile(r"<master\b[^>]*>[^<]*</master>")
_SLAVE = re.compile(r"<slave\b[^>]*?(?:/>|>\s*</slave>)")
_GROUP_ATTR = re.compile(r'\s(?:group|zone|zoneMaster|zoneSlave)="[^"]*"')
_ROOT = re.compile(r"(<SyncStatus\b[^>]*?)(/?>)")


@dataclass
class Member:
    """One player's place in the model."""

    master: Address | None = None
    slaves: list[Address] = field(default_factory=list)
    # Whose captured playback this player is playing; None when stopped.
    source: Address | None = None
    # Whether that playback can move to another player (a stream can, an
    # input on the player itself cannot).
    movable: bool = False
    fixed: bool = False
    sync_stat: int = 1000


class GroupModel:
    """Who leads whom, for every fake player, kept consistent."""

    def __init__(self, players: dict[Address, FakePlayer]) -> None:
        """Start from the topology the players were captured in."""
        self.players = players
        self.members: dict[Address, Member] = {}
        # Refuse every add, as a player does that cannot take more followers.
        self.refuse_adds = False
        # Report adds in two steps, as real leaders do.
        self.staged_adds = False
        # Followers a leader does not list yet, with reads left until it does.
        self._unlisted: dict[Address, tuple[Address, int]] = {}
        # Players whose responses no longer match their capture.
        self.changed: set[Address] = set()
        for address, player in players.items():
            body = player.captured("sync_status")
            member = Member()
            if match := re.search(r'<master port="(\d+)">([^<]+)</master>', body):
                member.master = (match.group(2), int(match.group(1)))
            member.slaves = [
                (ip, int(port))
                for ip, port in re.findall(r'<slave id="([^"]+)" port="(\d+)"', body)
            ]
            self.members[address] = member
        for address, member in self.members.items():
            member.source = self.root(address)

    # -- reading the model ----------------------------------------------------

    def root(self, address: Address) -> Address:
        """Return the player at the top of a player's group."""
        seen = {address}
        while (master := self.members[address].master) and master not in seen:
            seen.add(master)
            address = master
        return address

    def leader_of(self, address: Address) -> Address | None:
        """Return the player a player follows, if any."""
        return self.members[address].master

    def slaves_of(self, address: Address) -> list[Address]:
        """Return the players a player leads."""
        return list(self.members[address].slaves)

    def nested(self) -> list[Address]:
        """Players that both follow and lead: the state that must never arise."""
        return [
            address
            for address, member in self.members.items()
            if member.master and member.slaves
        ]

    # -- arranging a scenario -------------------------------------------------

    def arrange(self, groups: dict[Address, list[Address]]) -> None:
        """Set up groups before a test, everyone else on their own and stopped.

        Leaders keep playing what they were captured playing.
        """
        before = self._snapshot()
        for member in self.members.values():
            member.master, member.slaves, member.source = None, [], None
        for leader, slaves in groups.items():
            self.members[leader].source = leader
            for slave in slaves:
                self._attach(slave, leader)
        for address in self.members:
            if not groups.get(address) and self.members[address].master is None:
                # A player that is on its own keeps its own playback.
                self.members[address].source = address
        self._publish(before, push=False)

    def set_movable(self, address: Address, movable: bool = True) -> None:
        """Say whether a player's playback could move to another player."""
        self.members[address].movable = movable

    def set_fixed(self, address: Address) -> None:
        """Put a player in a fixed group (a stereo pair, say)."""
        before = self._snapshot()
        self.members[address].fixed = True
        self.changed.add(address)
        self._publish(before, push=False)

    # -- requests -------------------------------------------------------------

    def add_slave(self, receiver: Address, query: dict[str, str]) -> str:
        """Answer /AddSlave."""
        candidates = _addresses(query)
        if self.members[receiver].master is not None or self.refuse_adds:
            return "<addSlave></addSlave>"

        before = self._snapshot()
        accepted = []
        for candidate in candidates:
            if candidate == receiver or candidate not in self.members:
                continue
            member = self.members[candidate]
            if member.fixed:
                continue
            if member.master is not None:
                self._detach(candidate)
            if member.slaves:
                # A leader is taken as it is, its own group underneath.
                member.source = None
                for slave in member.slaves:
                    self.members[slave].source = None
            self._attach(candidate, receiver)
            accepted.append(candidate)
        if self.staged_adds and accepted:
            self._unlisted[receiver] = (accepted[-1], 1)
        self._publish(before)
        return (
            "<addSlave>"
            + "".join(
                f'<slave id="{ip}" port="{port}"></slave>' for ip, port in accepted
            )
            + "</addSlave>"
        )

    def remove_slave(self, receiver: Address, query: dict[str, str]) -> str:
        """Answer /RemoveSlave."""
        before = self._snapshot()
        member = self.members[receiver]
        for target in _addresses(query):
            if target == receiver:
                if not member.slaves:
                    return "<error>no slave available as new master</error>"
                root = self.members[self.root(receiver)]
                if root.source is not None and not root.movable:
                    return "<error>Cannot move input source</error>"
                self._hand_over(receiver)
            elif target in member.slaves:
                self._detach(target)
                self.members[target].source = None
        self._publish(before)
        return self.sync_status(receiver)

    def note_sync_read(self, address: Address) -> None:
        """Count a /SyncStatus request, completing a staged add when it is due."""
        if address not in self._unlisted:
            return
        follower, reads_left = self._unlisted[address]
        if reads_left > 0:
            self._unlisted[address] = (follower, reads_left - 1)
            return
        self._complete_add(address)

    def settle(self) -> None:
        """Complete every staged add now, as time passing would."""
        for address in list(self._unlisted):
            self._complete_add(address)

    def _complete_add(self, address: Address) -> None:
        del self._unlisted[address]
        # The rest of the add lands: only /SyncStatus says so.
        self.members[address].sync_stat += 1
        self.changed.add(address)
        self.players[address].push("/SyncStatus", self.sync_status(address))

    # -- responses ------------------------------------------------------------

    def sync_status(self, address: Address) -> str:
        """/SyncStatus as the player would now report it."""
        member = self.members[address]
        body = self.players[address].captured("sync_status")
        body = _MASTER.sub("", _SLAVE.sub("", body))
        body = re.sub(r'\b(syncStat|etag)="\d+"', rf'\1="{member.sync_stat}"', body)

        attributes = ""
        if member.master or member.slaves:
            root = self.root(address)
            attributes += f' group="{self._name(root)} + {self._group_size(root)}"'
        if member.fixed:
            attributes += ' zone="Pair" zoneSlave="true"'
        children = ""
        if member.master:
            children += f'<master port="{member.master[1]}">{member.master[0]}</master>'
        unlisted = self._unlisted.get(address, (None, 0))[0]
        for ip, port in member.slaves:
            if (ip, port) == unlisted:
                continue
            name = self._name((ip, port))
            children += f'<slave id="{ip}" port="{port}" name="{name}"></slave>'

        body = _GROUP_ATTR.sub("", body)
        return _ROOT.sub(
            lambda m: (
                m.group(1)
                + attributes
                + (
                    ">" + children + "</SyncStatus>"
                    if m.group(2) == "/>"
                    else ">" + children
                )
            ),
            body,
            count=1,
        )

    def status(self, address: Address) -> str:
        """/Status as the player would now report it."""
        root = self.root(address)
        source = self.members[root].source
        stat = self.members[root].sync_stat
        if source is None:
            return (
                f'<status etag="stopped-{stat}"><state>stop</state>'
                f"<syncStat>{stat}</syncStat></status>"
            )
        body = self.players[source].captured("status")
        body = re.sub(r"<groupName>[^<]*</groupName>", "", body)
        if self.members[root].slaves:
            group = f"{self._name(root)} + {self._group_size(root)}"
            body = body.replace("</status>", f"<groupName>{group}</groupName></status>")
        body = re.sub(r"<syncStat>\d+</syncStat>", f"<syncStat>{stat}</syncStat>", body)
        return re.sub(r'etag="[^"]*"', f'etag="{source[0]}-{stat}"', body, count=1)

    # -- internals ------------------------------------------------------------

    def _attach(self, candidate: Address, leader: Address) -> None:
        self.members[candidate].master = leader
        if candidate not in self.members[leader].slaves:
            self.members[leader].slaves.append(candidate)
        self.members[candidate].source = self.members[self.root(leader)].source

    def _detach(self, candidate: Address) -> None:
        master = self.members[candidate].master
        if master is not None and candidate in self.members[master].slaves:
            self.members[master].slaves.remove(candidate)
        self.members[candidate].master = None

    def _hand_over(self, leader: Address) -> None:
        """Take the leader out; the last listed player takes over the rest."""
        member = self.members[leader]
        rest = member.slaves
        member.slaves = []
        for slave in rest:
            self.members[slave].master = None
        if len(rest) >= 2:
            new_leader, others = rest[-1], rest[:-1]
            self.members[new_leader].source = member.source
            self.members[new_leader].movable = member.movable
            for other in others:
                self._attach(other, new_leader)
        else:
            for slave in rest:
                self.members[slave].source = None

    def _group_size(self, root: Address) -> int:
        return len(self.members[root].slaves)

    def _name(self, address: Address) -> str:
        body = self.players[address].captured("sync_status")
        match = re.search(r' name="([^"]*)"', body)
        return match.group(1) if match else f"{address[0]}"

    def _snapshot(self) -> dict[Address, tuple[str, str]]:
        return {
            address: (self._topology_key(address), self._playback_key(address))
            for address in self.members
        }

    def _topology_key(self, address: Address) -> str:
        member = self.members[address]
        return repr((member.master, member.slaves, member.fixed))

    def _playback_key(self, address: Address) -> str:
        root = self.root(address)
        return repr((root, self.members[root].source, self.members[root].slaves))

    def _publish(
        self, before: dict[Address, tuple[str, str]], push: bool = True
    ) -> None:
        """Bump what changed and push new /Status and /SyncStatus to long polls.

        A leader's /Status carries its syncStat, which is how the integration
        notices a regroup; followers see their leader's.
        """
        after = self._snapshot()
        moved = {address for address in after if after[address] != before[address]}
        if not moved:
            return
        for address in moved:
            self.members[address].sync_stat += 1
            self.changed.add(address)
        for address in self.members:
            root = self.root(address)
            if address in moved or root in moved:
                self.changed.add(address)
                if push:
                    self.players[address].push("/Status", self.status(address))
                    self.players[address].push("/SyncStatus", self.sync_status(address))


def _addresses(query: dict[str, str]) -> list[Address]:
    """Read slave=/port= or slaves=/ports= from a grouping request."""
    if "slaves" in query:
        ips = query["slaves"].split(",")
        ports = query.get("ports", "").split(",")
        return [
            (ip, int(ports[i]) if i < len(ports) and ports[i] else 11000)
            for i, ip in enumerate(ips)
            if ip
        ]
    if "slave" in query:
        return [(query["slave"], int(query.get("port") or 11000))]
    return []
