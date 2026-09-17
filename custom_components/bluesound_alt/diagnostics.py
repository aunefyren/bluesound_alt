"""Diagnostics for Bluesound Alt.

Diagnostics end up attached to public issues, so everything that identifies a
home is redacted: addresses, MACs, the names people give their players, and
account identifiers that streaming services put into stream URLs.
"""
from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import BluesoundConfigEntry

TO_REDACT = {
    CONF_HOST,
    "ip",
    "mac",
    "master_ip",
    "name",
    "group_name",
    "unique_id",
}

# TuneIn and other services append the player's serial or an account id to
# stream URLs, sometimes percent-encoded.
_ACCOUNT_PARAM = re.compile(
    r"(?i)((?:[?&;]|%(?:25)*(?:26|3f))"
    r"(?:serial|user(?:name|id|_id)?|account(?:id)?|email|"
    r"(?:access_|auth)?token|authtoken|session(?:id)?|password|pwd)"
    r"(?:=|%(?:25)*3d))"
    r"((?:(?!%(?:25)*(?:26|3f))[^&\s])+)"
)


def _scrub(value: Any) -> Any:
    """Redact account parameters inside every string, however nested."""
    if isinstance(value, str):
        return _ACCOUNT_PARAM.sub(r"\1**REDACTED**", value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _running(task: Any) -> bool:
    return task is not None and not task.done()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BluesoundConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    sync_info = coordinator.sync_info

    if coordinator.group_master_ip:
        role = "slave"
    elif coordinator.group_slaves:
        role = "master"
    else:
        role = "standalone"

    # Player, group and slave names are what people call their rooms. Source
    # and preset names ("Optical", a station) are kept: they are what source
    # problems are about, so those lists are only scrubbed, not redacted.
    identifying = async_redact_data(
        {
            "entry": {"data": dict(entry.data), "unique_id": entry.unique_id},
            "device": asdict(sync_info),
            "group": {
                "role": role,
                "master_ip": coordinator.group_master_ip,
                "slaves": coordinator.group_slaves,
            },
            "status": asdict(coordinator.data) if coordinator.data else None,
        },
        TO_REDACT,
    )
    return _scrub(
        {
            **identifying,
            "individual_volume": coordinator.individual_volume,
            "sources": coordinator.sources,
            "presets": coordinator.presets,
            "last_update_success": coordinator.last_update_success,
            "push_loop_running": _running(coordinator._long_poll_task),
            "volume_loop_running": _running(coordinator._volume_poll_task),
        }
    )
