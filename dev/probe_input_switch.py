#!/usr/bin/env python3
"""Find the command that switches the external inputs of a BluOS receiver.

Receivers with a BluOS module (NAD T758 and the like) list the amplifier's own
inputs in /SyncStatus:

    <externalSource id="4" name="Squeeze">
      <item id="1" name="BluRay"/> ... <item id="6" name="BluOS" isBluos="true"/>
    </externalSource>

The public API documents no command to switch between them, so this script
tries likely candidates one at a time and watches /SyncStatus to see whether
the active input actually changed. Nobody has to watch the receiver.

    python3 dev/probe_input_switch.py 192.168.1.50

Unlike probe_api.py this DOES change state: it switches the receiver to
another input (you pick which) and, once a working command is found, back
again. It never touches volume, power, grouping or settings. Turn the volume
down first in case the input it switches to is playing something loud.

One file is written, with IP and MAC addresses and the player name replaced:

  bluesound-input-switch-scrubbed.json

Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_PORT = 11000
REQUEST_TIMEOUT = 10
# An AVR can take a few seconds to switch, and a late switch must not be
# credited to the next candidate, so each one gets the full window.
SETTLE_SECONDS = 8
POLL_INTERVAL = 0.5
UA = "bluesound-alt-probe/0.1 (+https://github.com/aunefyren/bluesound_alt)"

# Settings whose id, name or label suggest they choose the active source.
SOURCE_SETTING = re.compile(r"(?i)extern|source|input")
NOT_SOURCE_SETTING = re.compile(r"(?i)bluetooth|autoplay|rename|hide|level|gain")


# -- HTTP -------------------------------------------------------------------


def get(host: str, port: int, path: str, params: dict, timeout: float) -> dict:
    """Send one GET and record what came back, errors included."""
    query = urllib.parse.urlencode(params)
    url = f"http://{host}:{port}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    record: dict = {"path": path, "params": params}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            record["status"] = resp.status
            record["body"] = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        record["status"] = err.code
        record["body"] = err.read().decode("utf-8", errors="replace")[:4000]
    except Exception as err:
        record["status"] = None
        record["error"] = repr(err)
    return record


def parse(record: dict) -> ET.Element | None:
    """Parse a recorded body, or None if it is not usable XML."""
    if record.get("status") != 200 or not record.get("body"):
        return None
    try:
        return ET.fromstring(record["body"])
    except ET.ParseError:
        return None


class Player:
    """One receiver, and what it has answered so far."""

    def __init__(self, host: str, port: int) -> None:
        """Remember where the receiver is."""
        self.host = host
        self.port = port
        self.reads: list[dict] = []

    def read(self, name: str, path: str, params: dict | None = None) -> dict:
        """Read-only request, kept for the report."""
        record = get(self.host, self.port, path, params or {}, REQUEST_TIMEOUT)
        record["name"] = name
        self.reads.append(record)
        return record

    def active_input(self) -> tuple[str | None, str | None]:
        """Return the id and name of the active external input."""
        record = get(self.host, self.port, "/SyncStatus", {}, REQUEST_TIMEOUT)
        root = parse(record)
        source = root.find("externalSource") if root is not None else None
        if source is None:
            return None, None
        return source.get("id"), source.get("name")

    def wait_for_input(self, before: str | None) -> tuple[str | None, float]:
        """Poll until the active input differs from before, or time runs out."""
        started = time.monotonic()
        current = before
        while time.monotonic() - started < SETTLE_SECONDS:
            time.sleep(POLL_INTERVAL)
            current, _ = self.active_input()
            if current != before:
                break
        return current, round(time.monotonic() - started, 1)


# -- candidates -------------------------------------------------------------


def setting_candidates(settings: list[ET.Element], target: dict) -> list[dict]:
    """Derive candidates from settings that look like a source chooser.

    BluOS settings are changed with GET <url>?<name>=<value>, as with the
    documented Bluetooth mode. If one of them picks the external source, its
    options name the inputs and carry the value to send.
    """
    found = []
    for root in settings:
        for setting in root.iter("setting"):
            label = " ".join(
                setting.get(k, "") for k in ("id", "name", "displayName")
            )
            url, name = setting.get("url"), setting.get("name")
            if not url or not name:
                continue
            if not SOURCE_SETTING.search(label) or NOT_SOURCE_SETTING.search(label):
                continue
            values = [
                option.get("value")
                for option in setting.iter()
                if option is not setting
                and option.get("value")
                and target["name"].lower()
                in " ".join(option.attrib.values()).lower()
            ]
            for value in values or [target["id"]]:
                found.append(
                    {"source": f"setting {setting.get('id')}", "path": url,
                     "params": {name: value}}
                )
    return found


def capture_candidates(capture: ET.Element | None, target: dict) -> list[dict]:
    """Documented input selection, if the target is among the Capture inputs."""
    found = []
    if capture is None:
        return found
    for item in capture.iter():
        text = item.get("text") or item.get("name") or ""
        url = item.get("URL") or item.get("url")
        if url and text.lower() == target["name"].lower():
            found.append(
                {"source": "RadioBrowse Capture", "path": "/Play",
                 "params": {"url": urllib.parse.unquote(url)}}
            )
    return found


def guessed_candidates(target: dict, position: int) -> list[dict]:
    """Undocumented guesses, most plausible first.

    position is the target's 1-based place in the externalSource list, which
    usually equals its id but need not.
    """
    tid = target["id"]
    guesses = [
        ("/Play", {"inputIndex": position}),  # documented for firmware 3.8-4.2
        ("/Play", {"InputId": tid}),  # as written in the API document's example
        ("/Play", {"inputId": tid}),
        ("/Play", {"externalSource": tid}),
        ("/Play", {"externalSourceId": tid}),
        ("/Play", {"url": f"Capture:External?id={tid}"}),
        ("/Play", {"url": "Capture:External", "id": tid}),
        ("/Play", {"url": f"Capture:External/{tid}"}),
        ("/ExternalSource", {"id": tid}),
        ("/SetExternalSource", {"id": tid}),
        ("/Source", {"id": tid}),
        ("/Input", {"id": tid}),
    ]
    if str(position) != str(tid):
        guesses.insert(1, ("/Play", {"inputIndex": tid}))
    return [{"source": "guess", "path": p, "params": q} for p, q in guesses]


def unique(candidates: list[dict]) -> list[dict]:
    """Drop repeats while keeping order."""
    seen, out = set(), []
    for c in candidates:
        key = (c["path"], tuple(sorted((k, str(v)) for k, v in c["params"].items())))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def retarget(candidate: dict, old: dict, new: dict, positions: dict) -> dict:
    """Aim the same command at another input, to switch back."""
    swap = {
        str(old["id"]): str(new["id"]),
        str(positions[old["id"]]): str(positions[new["id"]]),
        old["name"]: new["name"],
    }
    params = {}
    for key, value in candidate["params"].items():
        text = str(value)
        if key == "inputIndex":
            text = swap.get(text, text)
        else:
            text = re.sub(
                r"(?<![\w])(" + "|".join(map(re.escape, swap)) + r")(?![\w])",
                lambda m: swap[m.group(1)],
                text,
            )
        params[key] = text
    return {**candidate, "params": params}


# -- scrubbing --------------------------------------------------------------

IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
MAC = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f])")


def scrubber(player: Player):
    """Return a function that hides addresses and the player's own name."""
    names = []
    for record in player.reads:
        root = parse(record)
        if root is not None and root.tag == "SyncStatus":
            # A player left at its default name is called after its model.
            names += [
                n for n in (root.get("name"), root.get("group"))
                if n and n not in (root.get("model"), root.get("modelName"))
            ]

    def scrub(text: str) -> str:
        text = IPV4.sub("192.0.2.1", text)
        text = MAC.sub("00:00:5E:00:53:01", text)
        for name in names:
            if len(name) >= 3:
                text = text.replace(name, "Player 1")
        if not re.fullmatch(r"[\d.]+", player.host):
            text = text.replace(player.host, "player1.invalid")
        return text

    return scrub


def scrub_obj(obj, scrub):
    """Apply scrub to every string in a JSON-like structure."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(v, scrub) for v in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v, scrub) for k, v in obj.items()}
    return obj


# -- main -------------------------------------------------------------------


def split_host(value: str) -> tuple[str, int]:
    """Accept host or host:port."""
    host, _, port = value.partition(":")
    return host, int(port) if port else DEFAULT_PORT


def choose_target(inputs: list[dict], current: str, wanted: str | None) -> dict:
    """Pick the input to switch to: named, asked for, or the first sensible one."""
    choices = [i for i in inputs if i["id"] != current and not i["bluos"]]
    if not choices:
        sys.exit("No other external input to switch to.")
    if wanted:
        for i in inputs:
            if wanted.lower() in (i["name"].lower(), i["id"]):
                return i
        sys.exit(f"No input called {wanted!r}.")
    if not sys.stdin.isatty():
        return choices[0]
    print("\nWhich input may the script switch to while testing?")
    for n, i in enumerate(choices, 1):
        print(f"  {n}. {i['name']}")
    answer = input("Number [1]: ").strip() or "1"
    try:
        return choices[int(answer) - 1]
    except (ValueError, IndexError):
        sys.exit("No such choice.")


def main() -> int:
    """Discover the switching command and write the report."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("host", help="receiver address, host or host:port")
    parser.add_argument("--target", help="input to switch to while testing (name)")
    parser.add_argument("--yes", action="store_true", help="do not ask to confirm")
    parser.add_argument(
        "--all", action="store_true",
        help="keep trying after a command works, to find every one that does",
    )
    args = parser.parse_args()
    host, port = split_host(args.host)
    player = Player(host, port)

    # Read-only first: what the receiver says about its inputs and settings.
    sync = parse(player.read("sync_status", "/SyncStatus"))
    if sync is None:
        sys.exit(f"Could not reach a BluOS player at {host}:{port}.")
    player.read("status", "/Status")
    schema = sync.get("schemaVersion") or "32"
    capture = parse(player.read("capture", "/RadioBrowse", {"service": "Capture"}))
    settings = [
        root
        for root in (
            parse(player.read("settings_capture", "/Settings",
                              {"id": "capture", "schemaVersion": schema})),
            parse(player.read("settings_root", "/Settings",
                              {"schemaVersion": schema})),
        )
        if root is not None
    ]

    source = sync.find("externalSource")
    inputs = [
        {"id": item.get("id"), "name": item.get("name", ""),
         "bluos": item.get("isBluos") == "true"}
        for item in (source if source is not None else [])
    ]
    report: dict = {
        "probedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "model": sync.get("modelName") or sync.get("model"),
        "brand": sync.get("brand"),
        "version": sync.get("version"),
        "inputs": inputs,
        "attempts": [],
        "working": [],
    }

    if not inputs:
        print("This player lists no external inputs in /SyncStatus; nothing to try.")
    else:
        original = {"id": source.get("id"), "name": source.get("name", "")}
        positions = {i["id"]: n for n, i in enumerate(inputs, 1)}
        print(f"{report['brand']} {report['model']}, BluOS {report['version']}")
        print("Inputs: " + ", ".join(i["name"] for i in inputs))
        print(f"Active now: {original['name']}")
        target = choose_target(inputs, original["id"], args.target)
        report["original"], report["target"] = original, target

        candidates = unique(
            setting_candidates(settings, target)
            + capture_candidates(capture, target)
            + guessed_candidates(target, positions[target["id"]])
        )
        print(
            f"\nThe script will try up to {len(candidates)} commands to switch "
            f"from {original['name']} to {target['name']}, waiting up to "
            f"{SETTLE_SECONDS} seconds after each.\nIf one works it switches back "
            f"to {original['name']} afterwards. Turn the volume down first."
        )
        if (
            not args.yes
            and sys.stdin.isatty()
            and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes")
        ):
            print("Stopped; nothing was switched.")
            return 1

        current = original["id"]
        for n, cand in enumerate(candidates, 1):
            query = urllib.parse.urlencode(cand["params"])
            print(f"[{n}/{len(candidates)}] {cand['path']}?{query} ... ", end="",
                  flush=True)
            before = current
            record = get(host, port, cand["path"], cand["params"], REQUEST_TIMEOUT)
            after, waited = player.wait_for_input(before)
            current = after
            worked = after == target["id"]
            print("switched!" if worked else
                  f"changed to {after}" if after != before else "no change")
            report["attempts"].append({
                **cand, "status": record.get("status"),
                "body": (record.get("body") or record.get("error") or "")[:500],
                "before": before, "after": after, "waited": waited,
                "worked": worked,
            })
            if not worked:
                continue

            # Switching back with the same command confirms it was this one
            # and not a late reaction to an earlier attempt.
            back = retarget(cand, target, original, positions)
            get(host, port, back["path"], back["params"], REQUEST_TIMEOUT)
            current, _ = player.wait_for_input(target["id"])
            confirmed = current == original["id"]
            print(f"    switching back to {original['name']}: "
                  + ("confirmed" if confirmed else "did not work"))
            report["working"].append({**cand, "back": back, "confirmed": confirmed})
            if confirmed and not args.all:
                break

        if current != original["id"]:
            print(f"\nThe receiver is not on {original['name']} any more; "
                  "please switch it back in the BluOS app.")
        if report["working"]:
            w = report["working"][0]
            print(f"\nFound it: {w['path']}?{urllib.parse.urlencode(w['params'])}")
        else:
            print("\nNone of the commands switched the input. The report still "
                  "helps: it shows what the receiver answered to each one.")

    report["reads"] = player.reads
    scrubbed = scrub_obj(report, scrubber(player))
    path = "bluesound-input-switch-scrubbed.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scrubbed, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {path}. Please attach it to the GitHub issue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
