#!/usr/bin/env python3
"""Read-only probe of the BluOS HTTP API.

Captures the responses the integration depends on, so tests can run against
what real players actually return rather than what we imagine they return.
BluOS answers differ by model, firmware, streaming service and group role, so
run it once per situation worth capturing, each with its own label:

    python3 dev/probe_api.py 192.168.1.20 --label idle
    python3 dev/probe_api.py 192.168.1.20 --label spotify
    python3 dev/probe_api.py 192.168.1.20 --label airplay
    python3 dev/probe_api.py 192.168.1.20 192.168.1.21 --label grouped

Grouped players are followed automatically (master and slaves are probed
too), so one run captures a consistent view of the whole group.

Only GET requests that read state are sent. Nothing that plays, pauses,
changes volume or regroups is ever called.

Two files are written:

  bluesound-probe-<label>-raw.json        full responses -- keep private
  bluesound-probe-<label>-scrubbed.json   identifiers replaced -- shareable

The scrubbed file replaces IP addresses, MAC addresses, player and group
names, hostnames, e-mail addresses and account-like URL parameters with
stable fakes. Track, artist and station names are kept, since they are what
the tests exercise. Look it over before you share it.

Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import ipaddress
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
LONG_POLL_SECONDS = 3
MAX_PLAYERS = 10
# Below the top level, browse menus are mostly radio catalogue: large, and
# alike from one listing to the next. A few per service show the shape.
BROWSE_PER_SERVICE = 3
UA = "bluesound-alt-probe/0.1 (+https://github.com/aunefyren/bluesound_alt)"


# -- HTTP -------------------------------------------------------------------


def get(host: str, port: int, path: str, params: dict, timeout: float) -> dict:
    """Send one GET and record what came back, errors included."""
    query = urllib.parse.urlencode(params)
    url = f"http://{host}:{port}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    record: dict = {"path": path, "params": params}
    started = time.monotonic()
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
    record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return record


def parse(record: dict) -> ET.Element | None:
    """Parse a recorded body, or None if it is not usable XML."""
    if record.get("status") != 200 or not record.get("body"):
        return None
    try:
        return ET.fromstring(record["body"])
    except ET.ParseError:
        return None


# -- probing ----------------------------------------------------------------


def browse_children(root: ET.Element) -> list[str]:
    """Return navigable keys in a /Browse response.

    Mirrors the integration: browseKey, or the legacy radiotime @key on link
    items. Walks nested elements too, so category-grouped menus the
    integration does not handle yet still show up in the capture.
    """
    keys = []
    for item in root.iter("item"):
        key = item.get("browseKey")
        if not key and item.get("type") == "link":
            key = item.get("key")
        if key:
            keys.append(key)
    return keys


def probe_player(
    host: str, port: int, browse_depth: int, browse_limit: int
) -> tuple[list[dict], list[tuple[str, int]]]:
    """Capture one player. Returns its requests and any group peers found."""
    requests: list[dict] = []

    def call(name: str, path: str, params: dict | None = None, timeout=None):
        print(f"  {name}", file=sys.stderr)
        record = get(host, port, path, params or {}, timeout or REQUEST_TIMEOUT)
        record["name"] = name
        requests.append(record)
        time.sleep(0.2)  # be gentle with the player
        return record

    sync = call("sync_status", "/SyncStatus")
    status = call("status", "/Status")
    call("volume", "/Volume")
    call("presets", "/Presets")

    # One long-poll round, as the integration's push loop does it. With
    # nothing changing, the player holds the request until the timeout.
    status_root = parse(status)
    etag = status_root.get("etag") if status_root is not None else None
    if etag:
        call(
            "status_long_poll",
            "/Status",
            {"timeout": LONG_POLL_SECONDS, "etag": etag},
            timeout=LONG_POLL_SECONDS + REQUEST_TIMEOUT,
        )

    # Breadth-first through the browse tree, bounded so a large library or
    # streaming catalogue does not turn this into a crawl.
    root = parse(call("browse_root", "/Browse"))
    frontier = browse_children(root) if root is not None else []
    seen: set[str] = set()
    per_service: dict[str, int] = {}
    paged: set[str] = set()
    fetched = 0
    for depth in range(1, browse_depth + 1):
        next_frontier: list[str] = []
        for key in frontier:
            if key in seen or fetched >= browse_limit:
                continue
            service = key.split(":", 1)[0]
            if depth > 1 and per_service.get(service, 0) >= BROWSE_PER_SERVICE:
                continue
            per_service[service] = per_service.get(service, 0) + 1
            seen.add(key)
            fetched += 1
            name = f"browse_d{depth}_{fetched:02d}"
            node = parse(call(name, "/Browse", {"key": key}))
            if node is None:
                continue
            next_frontier.extend(browse_children(node))

            # Long lists arrive a page at a time. One follow-up page per
            # service is enough to show how continuation works.
            next_key = node.get("nextKey")
            if next_key and service not in paged:
                paged.add(service)
                call(f"{name}_page2", "/Browse", {"key": next_key})
        frontier = next_frontier

    peers: list[tuple[str, int]] = []
    sync_root = parse(sync)
    if sync_root is not None:
        master = sync_root.find("master")
        if master is not None and (master.text or "").strip():
            peers.append((master.text.strip(), int(master.get("port", DEFAULT_PORT))))
        for slave in sync_root.iter("slave"):
            if slave.get("id"):
                peers.append((slave.get("id"), int(slave.get("port", DEFAULT_PORT))))
    return requests, peers


# -- scrubbing --------------------------------------------------------------

# Values carried by these keys are left alone apart from the address patterns.
# They describe hardware and services, and player names can collide with them
# (a player left at its default name is called after its model).
KEEP_KEYS = {
    "brand",
    "class",
    "icon",
    "model",
    "modelName",
    "schemaVersion",
    "service",
    "serviceIcon",
    "serviceName",
    "state",
}

# Browse keys nest URLs inside URLs, so the same character can arrive
# percent-encoded several times over: ":" as %3A, %253A, %25253A and so on.
# Patterns below accept any depth of encoding for the separators they rely on.


def encoded(*hex_codes: str) -> str:
    """Match a character given by hex code, at any depth of percent-encoding."""
    return r"%(?:25)*(?:" + "|".join(hex_codes) + ")"


# A MAC may directly follow an encoded "=" (…serial%253D02%25253A11…), whose
# last character is itself a hex digit, so that prefix is matched explicitly.
MAC = re.compile(
    r"(" + encoded("3[dD]") + r"|(?<![0-9A-Fa-f]))"
    r"([0-9A-Fa-f]{2}(:|-|" + encoded("3[aA]") + r")[0-9A-Fa-f]{2}"
    r"(?:\3[0-9A-Fa-f]{2}){4})(?![0-9A-Fa-f])"
)
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ACCOUNT_PARAM = re.compile(
    r"(?i)((?:[?&;]|" + encoded("26", "3f") + r")"
    r"(?:serial|user(?:name|id|_id)?|account(?:id)?|email|"
    r"(?:access_|auth)?token|authtoken|session(?:id)?|password|pwd)"
    r"(?:=|" + encoded("3d") + r"))"
    r"((?:(?!" + encoded("26", "3f") + r")[^&\"'<\s])+)"
)
# BluOS Radio is served from a numbered Airable host. Whether the number
# identifies the account or only Bluesound as a partner is unknown, so it goes.
AIRABLE_HOST = re.compile(r"(?<!\d)\d{6,}(\.airable\.io)")
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')
ELEMENT = re.compile(r"<(\w+)>([^<]*)</\1>")

# 100.64.0.0/10 is carrier-grade NAT, which Python does not count as private.
CGNAT = ipaddress.ip_network("100.64.0.0/10")
FAKE_NET = ipaddress.ip_network("192.0.2.0/24")  # RFC 5737 documentation range


class Scrubber:
    """Replace identifiers with stable fakes, consistently across a run."""

    def __init__(self) -> None:
        """Start with no known identifiers."""
        self.ips: dict[str, str] = {}
        self.macs: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.hostnames: dict[str, str] = {}

    # Collection happens before any scrubbing so that every player's names
    # are known when the first body is rewritten.

    def learn_host(self, host: str) -> None:
        """Remember a host given on the command line or found in a group."""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            fake = f"player{len(self.hostnames) + 1}.invalid"
            self.hostnames.setdefault(host, fake)
        else:
            self.fake_ip(host, known=True)

    def learn_name(self, name: str | None) -> None:
        """Remember a player or group name."""
        name = (name or "").strip()
        if len(name) >= 3 and name not in self.names:
            self.names[name] = f"Player {len(self.names) + 1}"

    def learn_group(self, group: str | None) -> None:
        """Remember a group name by its parts.

        BluOS names a group after its master plus a count ("Office + 2"), or
        joins member names with "+". Learning the parts rather than the whole
        keeps the fake group name built from the fake player names.
        """
        for part in re.split(r"\s*\+\s*", group or ""):
            self.learn_name(part)

    def learn_mac(self, mac: str | None) -> None:
        """Remember a MAC address in every spelling it may turn up in."""
        match = MAC.fullmatch((mac or "").strip())
        if match:
            self.fake_mac(match.group(2))

    def learn(self, requests: list[dict]) -> None:
        """Harvest identifiers from one player's captured responses."""
        for record in requests:
            root = parse(record)
            if root is None:
                continue
            if record["name"] == "sync_status":
                self.learn_name(root.get("name"))
                self.learn_group(root.get("group"))
                self.learn_mac(root.get("mac"))
                for slave in root.iter("slave"):
                    self.learn_name(slave.get("name"))
            elif record["name"] == "status":
                self.learn_group(root.findtext("groupName"))

    # -- fakes ------------------------------------------------------------

    def fake_ip(self, value: str, known: bool = False) -> str:
        """Map a local or known address into the documentation range."""
        try:
            ip = ipaddress.IPv4Address(value)
        except ValueError:
            return value
        if ip in FAKE_NET:
            return value
        local = ip.is_private or ip.is_link_local or ip.is_loopback or ip in CGNAT
        if not (local or known) and value not in self.ips:
            return value  # public addresses belong to radio streams and CDNs
        if value not in self.ips:
            self.ips[value] = str(FAKE_NET[len(self.ips) + 1])
        return self.ips[value]

    def fake_mac(self, value: str) -> str:
        """Map a MAC address into the documentation range, keeping its style."""
        digits = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
        if digits.startswith("00005E0053"):
            return value
        if digits not in self.macs:
            self.macs[digits] = f"00005E0053{len(self.macs) + 1:02X}"
        fake = self.macs[digits]
        # Six octets and five separators of equal length, encoded or not.
        sep = value[2 : 2 + (len(value) - 12) // 5]
        octets = [fake[i : i + 2] for i in range(0, 12, 2)]
        if value[:2].islower():
            octets = [octet.lower() for octet in octets]
        return sep.join(octets)

    # -- rewriting --------------------------------------------------------

    def scrub_addresses(self, text: str) -> str:
        """Replace IP and MAC addresses."""
        text = MAC.sub(lambda m: m.group(1) + self.fake_mac(m.group(2)), text)
        text = IPV4.sub(lambda m: self.fake_ip(m.group(0)), text)
        text = AIRABLE_HOST.sub(r"0000000000\1", text)
        # A bare MAC (serial numbers, TuneIn URLs) only counts if it is known.
        for digits, fake in self.macs.items():
            text = re.sub(digits, fake, text, flags=re.IGNORECASE)
        return text

    def scrub_text(self, text: str, keep_names: bool = False) -> str:
        """Scrub free text: addresses, e-mails, account parameters, names."""
        text = ACCOUNT_PARAM.sub(r"\1REDACTED", text)
        text = EMAIL.sub("user@example.com", text)
        text = self.scrub_addresses(text)

        replacements = dict(self.hostnames)
        if not keep_names:
            replacements.update(self.names)
        for real in sorted(replacements, key=len, reverse=True):
            fake = replacements[real]
            for form in {
                real,
                html.escape(real),
                html.escape(real, quote=False)
                .replace('"', "&quot;")
                .replace("'", "&apos;"),
                urllib.parse.quote(real),
                urllib.parse.quote_plus(real),
            }:
                text = text.replace(form, fake)
        return text

    def scrub_xml(self, body: str) -> str:
        """Scrub an XML body in place, so its bytes stay otherwise untouched.

        Rewriting the text rather than re-serialising a parsed tree keeps
        escaping and percent-encoding exactly as the player sent them, which
        is precisely what encoding bugs depend on.
        """

        def value(key: str, raw: str) -> str:
            return self.scrub_text(raw, keep_names=key in KEEP_KEYS)

        body = ATTRIBUTE.sub(
            lambda m: f'{m.group(1)}="{value(m.group(1), m.group(2))}"', body
        )
        body = ELEMENT.sub(
            lambda m: f"<{m.group(1)}>{value(m.group(1), m.group(2))}</{m.group(1)}>",
            body,
        )
        # Anything the patterns above did not reach, such as mixed content.
        return self.scrub_addresses(body)

    def scrub_record(self, record: dict) -> dict:
        """Return a scrubbed copy of one captured request."""
        out = dict(record)
        out["params"] = {
            key: self.scrub_text(str(val)) for key, val in record["params"].items()
        }
        if "body" in record:
            out["body"] = self.scrub_xml(record["body"])
        if "error" in record:
            out["error"] = self.scrub_text(record["error"])
        return out

    def leaks(self, texts: list[str]) -> list[str]:
        """List known identifiers still present in scrubbed output."""
        text = "\n".join(texts)
        # Names may legitimately survive in model and service fields.
        named = ATTRIBUTE.sub(
            lambda m: "" if m.group(1) in KEEP_KEYS else m.group(0), text
        )
        named = ELEMENT.sub(
            lambda m: "" if m.group(1) in KEEP_KEYS else m.group(0), named
        )
        decoded = unquote_fully(text)
        bare = re.sub(r"[:-]", "", decoded)

        found = [
            ip
            for ip in self.ips
            if re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", text)
        ]
        found += [h for h in self.hostnames if h in text]
        found += [n for n in self.names if n in named]
        found += [m for m in self.macs if re.search(m, bare, re.IGNORECASE)]
        # Anything address-shaped outside the fake ranges, known or not.
        found += [
            m.group(2)
            for m in MAC.finditer(decoded)
            if not re.sub(r"[:-]", "", m.group(2)).upper().startswith("00005E0053")
        ]
        found += [
            m.group(0)
            for m in IPV4.finditer(decoded)
            if self.fake_ip(m.group(0)) != m.group(0)
        ]
        return found


def unquote_fully(text: str) -> str:
    """Undo percent-encoding however many times it was applied."""
    for _ in range(8):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


# -- main -------------------------------------------------------------------


def split_host(value: str) -> tuple[str, int]:
    """Accept host or host:port."""
    host, _, port = value.partition(":")
    return host, int(port) if port else DEFAULT_PORT


def probe_players(args: argparse.Namespace) -> list[dict]:
    """Probe the named players, and their group peers unless told not to."""
    queue = [split_host(h) for h in args.hosts]
    done: set[tuple[str, int]] = set()
    players: list[dict] = []

    while queue and len(players) < MAX_PLAYERS:
        host, port = queue.pop(0)
        if (host, port) in done:
            continue
        done.add((host, port))
        print(f"probing {host}:{port}", file=sys.stderr)
        requests, peers = probe_player(host, port, args.browse_depth, args.browse_limit)
        players.append({"host": host, "port": port, "requests": requests})
        if not args.no_follow_group:
            queue.extend(p for p in peers if p not in done)
    return players


def main() -> int:
    """Probe the given players and write the dumps."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("hosts", nargs="*", help="player address, host or host:port")
    parser.add_argument(
        "--rescrub",
        metavar="RAW_JSON",
        help="re-scrub an earlier raw dump instead of probing again",
    )
    parser.add_argument(
        "--label",
        default="snapshot",
        help="what the player is doing, e.g. idle, spotify, airplay, grouped",
    )
    parser.add_argument(
        "--browse-depth", type=int, default=2, help="levels below the browse root"
    )
    parser.add_argument(
        "--browse-limit", type=int, default=25, help="max browse requests per player"
    )
    parser.add_argument(
        "--no-follow-group",
        action="store_true",
        help="only probe the players named, not their group peers",
    )
    args = parser.parse_args()
    if bool(args.hosts) == bool(args.rescrub):
        parser.error("give either player addresses or --rescrub, not both")

    if args.rescrub:
        # Scrubbing improves over time; old captures should benefit without
        # anyone having to recreate the situation they were taken in.
        with open(args.rescrub, encoding="utf-8") as fh:
            raw = json.load(fh)
        label, players = raw["label"], raw["players"]
    else:
        label = re.sub(r"[^a-z0-9]+", "-", args.label.lower()).strip("-")
        label = label or "snapshot"
        players = probe_players(args)
        raw = {
            "probedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "scrubbed": False,
            "players": players,
        }

    scrubber = Scrubber()
    for player in players:
        scrubber.learn_host(player["host"])
        scrubber.learn(player["requests"])

    scrubbed = {
        "probedAt": raw["probedAt"],
        "label": label,
        "scrubbed": True,
        "players": [
            {
                "host": scrubber.scrub_text(p["host"]),
                "port": p["port"],
                "requests": [scrubber.scrub_record(r) for r in p["requests"]],
            }
            for p in players
        ],
    }

    raw_path = f"bluesound-probe-{label}-raw.json"
    scrubbed_path = f"bluesound-probe-{label}-scrubbed.json"
    if not args.rescrub:
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
    scrubbed_text = json.dumps(scrubbed, indent=2, ensure_ascii=False)
    with open(scrubbed_path, "w", encoding="utf-8") as fh:
        fh.write(scrubbed_text)

    failed = [
        f"{p['host']}:{p['port']} {r['name']}"
        for p in scrubbed["players"]
        for r in p["requests"]
        if r.get("status") != 200
    ]
    print(
        f"\n{len(players)} player(s); wrote {raw_path} (private) "
        f"and {scrubbed_path} (shareable)",
        file=sys.stderr,
    )
    if failed:
        print(f"non-200 responses: {', '.join(failed)}", file=sys.stderr)

    leaks = scrubber.leaks(
        [
            str(value)
            for player in scrubbed["players"]
            for record in player["requests"]
            for value in (
                player["host"],
                record.get("body", ""),
                record.get("error", ""),
                *record["params"].values(),
            )
        ]
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
