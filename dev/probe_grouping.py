#!/usr/bin/env python3
"""Probe how BluOS players regroup. THIS CHANGES GROUPING ON YOUR PLAYERS.

dev/probe_api.py only reads. This script deliberately groups and ungroups four
players to record how the firmware really behaves, including the cases that
break Home Assistant's grouping:

  - adding players one at a time and in a batch
  - removing a secondary player
  - asking a secondary player to add someone            (silently ignored?)
  - adding a player that is secondary in another group  (silently ignored?)
  - adding a player that leads its own group            (nests the groups?)
  - the leader of a 3-player group removing itself      (who leads after?)
  - a secondary player removing itself                  (does that work?)

After each change it polls /SyncStatus on the affected players until nothing
has changed for a few seconds, so the capture shows how long regrouping takes.
/Status is recorded before and after each step, to see what plays where.

Give four players, in this order:

    A  a player with something playing (ideally the one that usually leads)
    B  any player
    C  any player
    D  any player

    python3 dev/probe_grouping.py 10.0.0.20 10.0.0.21 10.0.0.22 10.0.0.23

A second, shorter scenario answers one question with music playing: when the
leader leaves its group, who leads the rest, and what do they all play? It
needs only A (playing) and two others:

    python3 dev/probe_grouping.py --scenario leader-leaves 10.0.0.20 10.0.0.21 \
        10.0.0.22

Existing groups among these players are dissolved first and restored at the
end, also after Ctrl-C. Playback on grouped players will be interrupted, and
for one step D's audio plays on all four. Each change asks for confirmation
first unless --yes is given.

Players in a fixed group (stereo pair, surround, subwoofer) are refused, and
so are groups that include players not named on the command line.

Writes, like dev/probe_api.py:

  bluesound-probe-grouping[-leader-leaves]-raw.json       keep private
  bluesound-probe-grouping[-leader-leaves]-scrubbed.json  shareable

Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import sys
import time

from probe_api import REQUEST_TIMEOUT, Scrubber, get, parse, split_host

ROLES = ("A", "B", "C", "D")
# How often to look while waiting for a change to land, how long nothing may
# change before a step counts as settled, and when to give up waiting.
POLL_SECONDS = 0.5
SETTLE_SECONDS = 3.0
MAX_WAIT_SECONDS = 20.0
# A second look at playback after a step, for players that start or stop late.
LATER_SECONDS = 10.0
PLAYING_STATES = ("play", "stream")

Address = tuple  # (host, port)


class Aborted(Exception):
    """The user declined a step."""


def one(player: Address) -> dict:
    """Parameters naming one player in /AddSlave or /RemoveSlave."""
    return {"slave": player[0], "port": player[1]}


def many(players: list) -> dict:
    """Parameters naming several players in one /AddSlave or /RemoveSlave."""
    return {
        "slaves": ",".join(p[0] for p in players),
        "ports": ",".join(str(p[1]) for p in players),
    }


def playback(record: dict) -> str:
    """Summarise a /Status answer as state and title."""
    root = parse(record)
    if root is None:
        return "unreadable"
    state = root.findtext("state") or "?"
    title = root.findtext("title1")
    return f"{state} {title}" if title else state


# -- topology ---------------------------------------------------------------


def topology(record: dict) -> dict | None:
    """Reduce a /SyncStatus answer to who leads whom."""
    root = parse(record)
    if root is None or root.tag != "SyncStatus":
        return None
    master = root.find("master")
    return {
        "master": (
            ((master.text or "").strip(), int(master.get("port", 11000)))
            if master is not None and (master.text or "").strip()
            else None
        ),
        "slaves": sorted(
            (slave.get("id", ""), int(slave.get("port", 11000)))
            for slave in root.iter("slave")
        ),
        "group": root.get("group"),
        "fixed": any(root.get(key) for key in ("zone", "zoneMaster", "zoneSlave")),
    }


class Session:
    """The players under test, and everything recorded about them."""

    def __init__(self, addresses: list, assume_yes: bool) -> None:
        """Remember the players and their roles."""
        # zip(strict=) needs Python 3.10; main() already checks there are four.
        self.role = dict(zip(addresses, ROLES))  # noqa: B905
        self.address = {role: addr for addr, role in self.role.items()}
        self.assume_yes = assume_yes
        self.steps: list[dict] = []

    # -- talking to players -------------------------------------------------

    def fetch(self, addr: Address, path: str, params: dict | None = None) -> dict:
        """GET from a player, keeping which player answered."""
        record = get(addr[0], addr[1], path, params or {}, REQUEST_TIMEOUT)
        record.update(host=addr[0], port=addr[1])
        return record

    def sync_status(self, addr: Address) -> dict:
        """Read one player's /SyncStatus, as a record named for the scrubber."""
        record = self.fetch(addr, "/SyncStatus")
        record["name"] = "sync_status"
        return record

    def status(self, addr: Address) -> dict:
        """Read one player's /Status, as a record named for the scrubber."""
        record = self.fetch(addr, "/Status")
        record["name"] = "status"
        return record

    def topologies(self) -> dict:
        """Read the current topology of every player under test."""
        return {addr: topology(self.sync_status(addr)) for addr in self.role}

    # -- describing ---------------------------------------------------------

    def name(self, addr: Address) -> str:
        """Refer to a player by role where it has one."""
        return self.role.get(tuple(addr), f"{addr[0]}:{addr[1]}")

    def describe(self, topo: dict | None) -> str:
        """Say in a few words what a player's topology is."""
        if topo is None:
            return "unreadable"
        parts = []
        if topo["master"]:
            parts.append(f"follows {self.name(topo['master'])}")
        if topo["slaves"]:
            parts.append("leads " + ", ".join(self.name(s) for s in topo["slaves"]))
        return " and ".join(parts) or "on its own"

    def show(self, topos: dict | None = None) -> None:
        """Print every player's topology."""
        for addr, topo in (topos or self.topologies()).items():
            print(f"    {self.role[addr]}: {self.describe(topo)}", file=sys.stderr)

    def show_playback(self, label: str, records: list) -> None:
        """Print what each player is playing."""
        summary = ", ".join(
            f"{self.name((r['host'], r['port']))} {playback(r)}" for r in records
        )
        print(f"  playing {label}: {summary}", file=sys.stderr)

    def wait_for_playback(self, addr: Address) -> None:
        """Make sure a player is playing before a step that needs it to."""
        while True:
            now = playback(self.status(addr))
            if now.split(" ")[0] in PLAYING_STATES:
                return
            role = self.role[addr]
            if self.assume_yes:
                print(f"  note: {role} is not playing ({now})", file=sys.stderr)
                return
            answer = input(
                f"  {role} is not playing ({now}). Start playback on {role} and "
                "press Enter, or type s to carry on without: "
            )
            if answer.strip().lower() == "s":
                return

    # -- steps --------------------------------------------------------------

    def confirm(self, prompt: str) -> None:
        """Ask before changing anything."""
        if self.assume_yes:
            return
        answer = input(f"{prompt} [y/N] ").strip().lower()
        if answer != "y":
            raise Aborted

    def watch(self, addrs: list) -> tuple[list, bool]:
        """Poll /SyncStatus until the players' topology stops changing.

        Records a snapshot only when a player's topology differs from its
        previous one, with milliseconds since the change was requested.
        """
        started = time.monotonic()
        last: dict = {}
        last_change = started
        timeline: list[dict] = []
        while True:
            for addr in addrs:
                record = self.sync_status(addr)
                seen = (record.get("status"), topology(record))
                if last.get(addr) != seen:
                    last[addr] = seen
                    last_change = time.monotonic()
                    record["t_ms"] = round((last_change - started) * 1000)
                    timeline.append(record)
            now = time.monotonic()
            if now - last_change >= SETTLE_SECONDS:
                return timeline, True
            if now - started >= MAX_WAIT_SECONDS:
                return timeline, False
            time.sleep(POLL_SECONDS)

    def step(
        self,
        name: str,
        description: str,
        sender: Address,
        path: str,
        params: dict,
        confirm: bool = True,
        later: float = 0,
    ) -> dict:
        """Send one grouping request and record what it did to every player."""
        print(f"\n[{name}] {description}", file=sys.stderr)
        print(f"  GET {self.name(sender)} {path} {params}", file=sys.stderr)
        if confirm:
            self.confirm("  send it?")

        watched = list(self.role)
        status_before = [self.status(addr) for addr in watched]
        action = self.fetch(sender, path, params)
        action["name"] = name
        timeline, settled = self.watch(watched)
        status_after = [self.status(addr) for addr in watched]

        print(
            f"  HTTP {action.get('status')}, "
            f"{'settled' if settled else 'still changing'} after "
            f"{timeline[-1]['t_ms'] if timeline else 0} ms:",
            file=sys.stderr,
        )
        self.show()
        self.show_playback("before", status_before)
        self.show_playback("after", status_after)
        step = {
            "name": name,
            "description": description,
            "action": action,
            "timeline": timeline,
            "settled": settled,
            "status_before": status_before,
            "status_after": status_after,
        }
        if later:
            print(f"  waiting {later:.0f} s to look again...", file=sys.stderr)
            time.sleep(later)
            step["status_later"] = [self.status(addr) for addr in watched]
            self.show_playback(f"{later:.0f} s later", step["status_later"])
        self.steps.append(step)
        return step

    # -- group operations ---------------------------------------------------

    def dissolve(self, prefix: str, confirm: bool = True) -> None:
        """Ungroup every player under test, a leader at a time.

        Re-reads the topology after each removal: taking apart a nested chain
        changes who leads what along the way.
        """
        for _ in range(len(self.role) * 2):
            leaders = [
                (addr, topo)
                for addr, topo in self.topologies().items()
                if topo and topo["slaves"]
            ]
            if not leaders:
                return
            addr, topo = leaders[0]
            self.step(
                f"{prefix}_{self.role[addr]}",
                f"{self.role[addr]} removes all its secondaries in one request",
                addr,
                "/RemoveSlave",
                {
                    "slaves": ",".join(s[0] for s in topo["slaves"]),
                    "ports": ",".join(str(s[1]) for s in topo["slaves"]),
                },
                confirm=confirm,
            )
        print("  gave up: players still grouped after removing", file=sys.stderr)

    def regroup(self, original: dict, confirm: bool = True) -> None:
        """Put the players back into the groups they started in."""
        self.dissolve("restore_dissolve", confirm=confirm)
        for addr, topo in original.items():
            if topo and topo["slaves"] and not topo["master"]:
                self.step(
                    f"restore_{self.role[addr]}",
                    f"{self.role[addr]} takes back its original secondaries",
                    addr,
                    "/AddSlave",
                    {
                        "slaves": ",".join(s[0] for s in topo["slaves"]),
                        "ports": ",".join(str(s[1]) for s in topo["slaves"]),
                    },
                    confirm=confirm,
                )


# -- the experiment ---------------------------------------------------------


def shape(topos: dict) -> dict:
    """Who leads whom, ignoring group names, which BluOS may rename."""
    return {
        addr: (topo["master"], topo["slaves"]) if topo else None
        for addr, topo in topos.items()
    }


def check_players(session: Session) -> dict:
    """Refuse setups this script cannot safely undo."""
    original = session.topologies()
    known = set(session.role)
    for addr, topo in original.items():
        role = session.role[addr]
        if topo is None:
            raise SystemExit(f"{role} ({addr[0]}) did not answer /SyncStatus")
        if topo["fixed"]:
            raise SystemExit(f"{role} is in a fixed group; not touching it")
        others = set(topo["slaves"]) | ({topo["master"]} if topo["master"] else set())
        strangers = {o for o in others if tuple(o) not in known}
        if strangers:
            raise SystemExit(
                f"{role} is grouped with a player not given here "
                f"({', '.join(h for h, _ in strangers)}); include it or ungroup it"
            )
    return original


def run(session: Session) -> None:
    """Walk the players through every grouping case worth capturing."""
    a, b, c, d = (session.address[r] for r in ROLES)

    def add(receiver, *players, name, description):
        params = one(players[0]) if len(players) == 1 else many(players)
        return session.step(name, description, receiver, "/AddSlave", params)

    def remove(receiver, player, name, description):
        return session.step(name, description, receiver, "/RemoveSlave", one(player))

    session.dissolve("setup_dissolve")

    add(a, b, name="add_single", description="A adds B")
    add(a, c, d, name="add_batched", description="A adds C and D in one request")
    remove(a, d, name="remove_secondary", description="A removes D")
    add(
        b,
        d,
        name="add_to_secondary",
        description="B, which follows A, is asked to add D",
    )
    add(
        d,
        c,
        name="add_secondary_of_other_group",
        description="D is asked to add C, which follows A",
    )
    add(
        d,
        a,
        name="add_leader_of_other_group",
        description="D is asked to add A, which leads B and C",
    )
    if session.topologies()[a]["master"] == d:
        remove(
            d,
            a,
            name="leader_removes_nested_leader",
            description="D removes A; does A still lead its own group after?",
        )
    session.dissolve("recover")
    add(a, b, c, name="regroup_three", description="A leads B and C again")
    remove(
        a,
        a,
        name="leader_removes_itself",
        description="A, leading B and C, removes itself (who leads, what plays?)",
    )

    topos = session.topologies()
    follower = next(
        (addr for addr in (b, c) if topos[addr] and topos[addr]["master"]), None
    )
    if follower:
        remove(
            follower,
            follower,
            name="secondary_removes_itself",
            description=f"{session.role[follower]} asks itself to remove itself",
        )


def run_leader_leaves(session: Session) -> None:
    """With music playing, have a group's leader leave: of three, then of two."""
    a, b, c = (session.address[r] for r in ROLES[:3])

    session.dissolve("setup_dissolve")
    session.wait_for_playback(a)
    session.step("group_three", "A leads B and C", a, "/AddSlave", many([b, c]))
    session.wait_for_playback(a)
    session.step(
        "leader_of_three_leaves",
        "A, playing and leading B and C, removes itself",
        a,
        "/RemoveSlave",
        one(a),
        later=LATER_SECONDS,
    )

    session.dissolve("regroup_dissolve")
    session.wait_for_playback(a)
    session.step("group_two", "A leads B", a, "/AddSlave", one(b))
    session.wait_for_playback(a)
    session.step(
        "leader_of_two_leaves",
        "A, playing and leading only B, removes itself",
        a,
        "/RemoveSlave",
        one(a),
        later=LATER_SECONDS,
    )


# name: (players needed, how to run it, capture label)
SCENARIOS = {
    "all": (4, run, "grouping"),
    "leader-leaves": (3, run_leader_leaves, "grouping-leader-leaves"),
}


def scrub(raw: dict) -> tuple[dict, list]:
    """Scrub a raw grouping dump; return it with any identifiers that survived."""
    scrubber = Scrubber()
    records = [r for p in raw["players"] for r in p["requests"]]
    for step in raw["steps"]:
        records.extend(step["timeline"])
        records.extend(step["status_before"])
        records.extend(step["status_after"])
        records.extend(step.get("status_later", []))
    for player in raw["players"]:
        scrubber.learn_host(player["host"])
    scrubber.learn(records)

    def clean(record: dict) -> dict:
        out = scrubber.scrub_record(record)
        if "host" in record:
            out["host"] = scrubber.scrub_text(record["host"])
        return out

    scrubbed = {
        "probedAt": raw["probedAt"],
        "label": raw["label"],
        "scrubbed": True,
        "players": [
            {
                "host": scrubber.scrub_text(p["host"]),
                "port": p["port"],
                "role": p["role"],
                "requests": [clean(r) for r in p["requests"]],
            }
            for p in raw["players"]
        ],
        "steps": [
            {
                **step,
                "action": clean(step["action"]),
                "timeline": [clean(r) for r in step["timeline"]],
                "status_before": [clean(r) for r in step["status_before"]],
                "status_after": [clean(r) for r in step["status_after"]],
                **(
                    {"status_later": [clean(r) for r in step["status_later"]]}
                    if "status_later" in step
                    else {}
                ),
            }
            for step in raw["steps"]
        ],
    }

    texts: list[str] = []
    everything = [r for p in scrubbed["players"] for r in p["requests"]]
    for step in scrubbed["steps"]:
        everything.append(step["action"])
        for key in ("timeline", "status_before", "status_after", "status_later"):
            everything.extend(step.get(key, []))
    for record in everything:
        texts.extend(
            [
                record.get("host", ""),
                record.get("body", ""),
                record.get("error", ""),
                *map(str, record["params"].values()),
            ]
        )
    return scrubbed, scrubber.leaks(texts)


def main() -> int:
    """Run the experiment and write the dumps."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("hosts", nargs="*", help="players A B C D, host or host:port")
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="all",
        help="all grouping cases (4 players), or leader-leaves (3 players)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask before each change"
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="leave the players as the experiment ends instead of regrouping",
    )
    parser.add_argument(
        "--rescrub",
        metavar="RAW_JSON",
        help="re-scrub an earlier raw dump instead of probing again",
    )
    args = parser.parse_args()

    if args.rescrub:
        if args.hosts:
            parser.error("give either player addresses or --rescrub, not both")
        with open(args.rescrub, encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        needed, scenario, label = SCENARIOS[args.scenario]
        if len(args.hosts) != needed:
            roles = " ".join(ROLES[:needed])
            parser.error(f"{args.scenario} needs exactly {needed} players: {roles}")
        addresses = [split_host(h) for h in args.hosts]
        if len(set(addresses)) != needed:
            parser.error("the players must all be different")
        for host, _ in addresses:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                # Groups refer to players by IP; a hostname would not match.
                parser.error(f"give players by IP address, not {host!r}")

        session = Session(addresses, args.yes)
        original = check_players(session)
        print("Starting topology:", file=sys.stderr)
        session.show(original)
        baseline = [
            {
                "host": addr[0],
                "port": addr[1],
                "role": role,
                "requests": [session.sync_status(addr), session.status(addr)],
            }
            for addr, role in session.role.items()
        ]

        completed = False
        try:
            scenario(session)
            completed = True
        except Aborted:
            print("\nStopped at your request.", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
        finally:
            if not args.no_restore:
                print("\nRestoring the original groups...", file=sys.stderr)
                try:
                    session.regroup(original, confirm=False)
                except KeyboardInterrupt:
                    print("Restore interrupted.", file=sys.stderr)
                final = session.topologies()
                if shape(final) == shape(original):
                    print("Players are back as they were.", file=sys.stderr)
                else:
                    print(
                        "WARNING: players are NOT back as they were. Now:",
                        file=sys.stderr,
                    )
                    session.show(final)

        raw = {
            "probedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "scrubbed": False,
            "completed": completed,
            "players": baseline,
            "steps": session.steps,
        }

    scrubbed, leaks = scrub(raw)
    scrubbed["completed"] = raw.get("completed", False)
    raw_path = f"bluesound-probe-{raw['label']}-raw.json"
    scrubbed_path = f"bluesound-probe-{raw['label']}-scrubbed.json"
    if not args.rescrub:
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
    with open(scrubbed_path, "w", encoding="utf-8") as fh:
        json.dump(scrubbed, fh, indent=2, ensure_ascii=False)

    print(
        f"\n{len(raw['steps'])} step(s); wrote {raw_path} (private) "
        f"and {scrubbed_path} (shareable)",
        file=sys.stderr,
    )
    if leaks:
        print(
            f"WARNING: {len(set(leaks))} identifier(s) survived scrubbing. Review "
            f"{scrubbed_path} before sharing it.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
