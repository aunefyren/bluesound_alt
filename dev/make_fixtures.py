#!/usr/bin/env python3
"""Turn scrubbed probe dumps into test fixtures.

Reads one or more bluesound-probe-<label>-scrubbed.json files written by
dev/probe_api.py and lays each out as plain XML files, one per response:

    tests/fixtures/<label>/<host>_<port>/sync_status.xml
    tests/fixtures/<label>/<host>_<port>/status.xml
    tests/fixtures/<label>/<host>_<port>/browse_d1_01.xml
    ...
    tests/fixtures/<label>/<host>_<port>/index.json

index.json maps each request (path and query parameters) to its file and
HTTP status, so a fake session can answer exactly what the player answered.

Usage:
    python3 dev/make_fixtures.py                      # every dump in cwd
    python3 dev/make_fixtures.py bluesound-probe-airplay-scrubbed.json
"""

from __future__ import annotations

import ipaddress
import json
import pathlib
import re
import shutil
import sys

from probe_api import BROWSE_PER_SERVICE, FAKE_NET, IPV4, MAC, unquote_fully

FIXTURES = pathlib.Path("tests/fixtures")
DEEP_BROWSE = re.compile(r"^browse_d([2-9]|\d{2,})_")


def suspicious(text: str) -> list[str]:
    """Find addresses that the probe's scrubber should have replaced.

    A last line of defence before anything lands in the repository: local
    addresses outside the documentation range, and MACs outside theirs.
    """
    text = unquote_fully(text)
    found = []
    for match in IPV4.finditer(text):
        try:
            ip = ipaddress.IPv4Address(match.group(0))
        except ValueError:
            continue
        if ip not in FAKE_NET and (ip.is_private or ip.is_link_local):
            found.append(match.group(0))
    for match in MAC.finditer(text):
        digits = re.sub(r"[:-]", "", match.group(2)).upper()
        if not digits.startswith("00005E0053"):
            found.append(match.group(2))
    return found


def keep(record: dict, per_service: dict[str, int]) -> bool:
    """Apply the probe's per-service browse cap, for dumps taken without it."""
    if not DEEP_BROWSE.match(record["name"]) or record["name"].endswith("_page2"):
        return True
    service = str(record["params"].get("key", "")).split(":", 1)[0]
    per_service[service] = per_service.get(service, 0) + 1
    return per_service[service] <= BROWSE_PER_SERVICE


def write_dump(src: pathlib.Path) -> bool:
    """Write the fixtures for one dump. Returns False if it was refused."""
    dump = json.loads(src.read_text(encoding="utf-8"))
    if dump.get("scrubbed") is not True:
        print(f"refusing {src}: not a scrubbed dump", file=sys.stderr)
        return False

    leaks = suspicious(json.dumps(dump, ensure_ascii=False))
    if leaks:
        print(
            f"refusing {src}: unscrubbed addresses {sorted(set(leaks))}",
            file=sys.stderr,
        )
        return False

    label = dump["label"]
    label_dir = FIXTURES / label
    # A re-probe replaces the scenario wholesale, so no stale files linger.
    if label_dir.exists():
        shutil.rmtree(label_dir)

    for player in dump["players"]:
        player_dir = label_dir / f"{player['host']}_{player['port']}"
        player_dir.mkdir(parents=True)
        index = []
        per_service: dict[str, int] = {}
        for record in player["requests"]:
            if not keep(record, per_service):
                continue
            entry = {
                "name": record["name"],
                "path": record["path"],
                "params": record["params"],
                "status": record["status"],
            }
            if "body" in record:
                entry["file"] = f"{record['name']}.xml"
                (player_dir / entry["file"]).write_text(
                    record["body"], encoding="utf-8"
                )
            else:
                entry["error"] = record.get("error")
            index.append(entry)

        (player_dir / "index.json").write_text(
            json.dumps(
                {
                    "label": label,
                    "probedAt": dump["probedAt"],
                    "host": player["host"],
                    "port": player["port"],
                    **({"role": player["role"]} if "role" in player else {}),
                    "requests": index,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {player_dir} ({len(index)} responses)")

    # Grouping captures (dev/probe_grouping.py) are a sequence of changes, each
    # with what every player reported as it settled; kept whole, in order.
    if "steps" in dump:
        steps_file = label_dir / "steps.json"
        steps_file.write_text(
            json.dumps(
                {"probedAt": dump["probedAt"], "steps": dump["steps"]},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {steps_file} ({len(dump['steps'])} steps)")
    return True


def main() -> int:
    """Write fixtures for every dump given, or every dump found."""
    sources = [pathlib.Path(arg) for arg in sys.argv[1:]] or sorted(
        pathlib.Path().glob("bluesound-probe-*-scrubbed.json")
    )
    if not sources:
        print("no scrubbed dumps found; run dev/probe_api.py first", file=sys.stderr)
        return 1

    ok = True
    for src in sources:
        ok = write_dump(src) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
