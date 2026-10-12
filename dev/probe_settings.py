#!/usr/bin/env python3
"""Map a BluOS player's settings, to find its sound profile and reboot commands.

The BluOS app builds its settings screens from the player's /Settings tree:
menus that link to further menus by id, and settings that name the URL and
parameter that change them. The public API documents neither the soundbar's
sound profile nor rebooting, so this script reads the whole tree and lists
what looks relevant, with the command each one would send.

    python3 dev/probe_settings.py 192.168.1.50

It only reads. It follows /Settings?id=... links and nothing else on port
11000, never requests a setting's own URL (that is what changes it), and on
the player's web page (port 80) fetches only the front page, to see how its
reboot form submits. Nothing is changed and the player keeps playing.

One file is written, with addresses, MAC addresses, player names and account
details replaced:

  bluesound-settings-scrubbed.json

Run it from the dev directory or anywhere else; it needs probe_api.py next to
it. Standard library only, Python 3.8+.
"""

from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
import json
import pathlib
import re
import sys
from typing import Callable
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from probe_api import REQUEST_TIMEOUT, Scrubber, get, parse, split_host

WEB_PORT = 80
# A settings tree has a few dozen menus; this only stops a runaway walk.
MAX_MENUS = 200
OUTPUT = "bluesound-settings-scrubbed.json"

# Settings and menus worth pointing out, by id, name or label.
INTERESTING = re.compile(
    r"(?i)profile|mode|preset|eq|tone|sound|audio|dialog|night|bass|treble"
    r"|loud|reboot|restart|power|standby"
)
LABEL_KEYS = ("id", "name", "displayName", "description", "url")

Reader = Callable[[str, str, dict], dict]


def walk_settings(read: Reader, schema: str) -> list[dict]:
    """Read the root settings menu and every menu reachable from it by id.

    Only /Settings is ever requested. A setting's url attribute is where the
    app sends a change, so it is recorded but never followed.
    """
    records = []
    seen: set[str] = set()
    queue: deque[str | None] = deque([None])
    while queue and len(records) < MAX_MENUS:
        menu = queue.popleft()
        params = {"schemaVersion": schema}
        if menu is not None:
            params = {"id": menu, **params}
        record = read(f"settings_{menu or 'root'}", "/Settings", params)
        records.append(record)
        root = parse(record)
        if root is None:
            continue
        for element in root.iter():
            child = element.get("id")
            if child and child != menu and child not in seen:
                seen.add(child)
                queue.append(child)
    return records


def describe(element: ET.Element, menu: str) -> dict:
    """Summarise one setting or menu: what it is and how it would be changed."""
    out = {"menu": menu, "tag": element.tag}
    out.update({k: v for k, v in element.attrib.items()})
    # A setting's choices are <value name=... displayName=...> children.
    options = [dict(value.attrib) for value in element if value.tag == "value"]
    if options:
        out["options"] = options
    if element.get("url") and element.get("name"):
        # As the BluOS app sends it: a JSON body naming the setting.
        out["changeWith"] = (
            f'POST {element.get("url")} {{"{element.get("name")}": <value>}}'
        )
    return out


def interesting(records: list[dict]) -> list[dict]:
    """List the settings and menus whose labels suggest sound or power."""
    found = []
    for record in records:
        root = parse(record)
        if root is None:
            continue
        menu = record["params"].get("id", "root")
        for element in root.iter():
            if element.tag == "value":
                continue  # listed with the setting it belongs to
            label = " ".join(element.get(k, "") for k in LABEL_KEYS)
            if label.strip() and INTERESTING.search(label):
                found.append(describe(element, menu))
    return found


def web_forms(record: dict) -> list[dict]:
    """Pull forms and links out of the player's web page, without running them."""
    body = record.get("body") or ""
    forms = []
    for match in re.finditer(r"(?is)(<form\b[^>]*>)(.*?)</form>", body):
        tag, inner = match.groups()
        method = re.search(r"(?i)method=[\"']?(\w+)", tag)
        action = re.search(r"(?i)action=[\"']?([^\"' >]+)", tag)
        forms.append(
            {
                "method": method.group(1) if method else "GET",
                "action": action.group(1) if action else "",
                # name=value of each field and button, as the form would send.
                "fields": [
                    f"{n}={v}"
                    for n, v in re.findall(
                        r"(?is)<(?:input|button|select)\b[^>]*?name=[\"']?([^\"' >]+)"
                        r"(?:[^>]*?value=[\"']?([^\"'>]*))?",
                        inner,
                    )
                ],
            }
        )
    links = sorted(set(re.findall(r"(?i)href=[\"']?([^\"' >]+)", body)))
    return [{"forms": forms, "links": links}]


def settings_links(links: list[str]) -> list[dict]:
    """Return the web page's settings pages as query parameters (display only)."""
    found = []
    for link in links:
        path, _, query = link.partition("?")
        if path == "/settings" and query:
            found.append(dict(urllib.parse.parse_qsl(query)))
    return found


def scrub(host: str, records: list[dict], report: dict) -> tuple[dict, list[str]]:
    """Scrub the report; return it with any identifiers that survived."""
    scrubber = Scrubber()
    scrubber.learn_host(host)
    scrubber.learn(records)

    def clean(obj):
        if isinstance(obj, str):
            return scrubber.scrub_text(obj)
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        return obj

    scrubbed = {
        **clean({k: v for k, v in report.items() if k != "requests"}),
        "scrubbed": True,
        "requests": [scrubber.scrub_record(r) for r in records],
    }
    # Bodies as sent, not JSON-escaped, so the leak check can read their XML.
    texts = [json.dumps({k: v for k, v in scrubbed.items() if k != "requests"})]
    for record in scrubbed["requests"]:
        texts += [record.get("body", ""), record.get("error", "")]
        texts += map(str, record["params"].values())
    return scrubbed, scrubber.leaks(texts)


def main() -> int:
    """Walk the settings tree and write the report."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("host", help="player address, host or host:port")
    parser.add_argument(
        "--no-web", action="store_true", help="skip the player's web page (port 80)"
    )
    args = parser.parse_args()
    host, port = split_host(args.host)
    records: list[dict] = []

    def read(name: str, path: str, params: dict, on_port: int = port) -> dict:
        record = get(host, on_port, path, params, REQUEST_TIMEOUT)
        record["name"] = name
        record["port"] = on_port
        records.append(record)
        return record

    sync = parse(read("sync_status", "/SyncStatus", {}))
    if sync is None:
        sys.exit(f"Could not reach a BluOS player at {host}:{port}.")
    read("status", "/Status", {})
    schema = sync.get("schemaVersion") or "32"
    model = sync.get("modelName") or sync.get("model")
    print(f"{sync.get('brand')} {model}, BluOS {sync.get('version')}")

    menus = walk_settings(read, schema)
    answered = [r for r in menus if parse(r) is not None]
    print(f"Read {len(answered)} of {len(menus)} settings menus.")

    report: dict = {
        "probedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "brand": sync.get("brand"),
        "version": sync.get("version"),
        "schemaVersion": schema,
        "interesting": interesting(menus),
    }
    if not args.no_web:
        report["webPage"] = web_forms(read("web_root", "/", {}, on_port=WEB_PORT))
        # Pages such as Power are linked from the web page but not the app's
        # tree. Read each page, and the app's menu of the same id if any.
        walked = {r["params"].get("id") for r in menus}
        for params in settings_links(report["webPage"][0]["links"]):
            page = "_".join(params.values())
            report["webPage"] += web_forms(
                read(f"web_{page}", "/settings", params, on_port=WEB_PORT)
            )
            for menu in {params.get("id"), params.get("page_id")} - walked - {None}:
                walked.add(menu)
                menus.append(
                    read(
                        f"settings_{menu}",
                        "/Settings",
                        {"id": menu, "schemaVersion": schema},
                    )
                )
        report["interesting"] = interesting(menus)

    scrubbed, leaks = scrub(host, records, report)
    out = pathlib.Path(OUTPUT)
    out.write_text(json.dumps(scrubbed, indent=2, ensure_ascii=False), "utf-8")

    print("\nSettings that look like sound or power controls:")
    for item in scrubbed["interesting"]:
        label = item.get("displayName") or item.get("name") or item.get("id")
        line = f"  [{item['menu']}] {label}"
        if "value" in item:
            line += f" = {item['value']}"
        if "changeWith" in item:
            line += f"  ({item['changeWith']})"
        print(line)
        for option in item.get("options", []):
            text = option.get("displayName") or option.get("name") or ""
            value = option.get("name") or option.get("value") or ""
            print(f"      {value}: {text}")
    if not scrubbed["interesting"]:
        print("  none found; the full tree is in the file.")
    for page in scrubbed.get("webPage", []):
        for form in page["forms"]:
            fields = ", ".join(form.get("fields", []))
            print(f"Web page form: {form['method'].upper()} {form['action']} {fields}")

    print(f"\nWrote {out}.")
    if leaks:
        print(
            "WARNING: these identifiers may still be in the file, check before "
            "sharing: " + ", ".join(sorted(set(leaks)))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
