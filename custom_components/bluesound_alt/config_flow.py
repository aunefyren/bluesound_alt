"""Config flow for Bluesound Alt."""
from __future__ import annotations

import logging
from typing import Any
from xml.parsers.expat import ExpatError

import aiohttp
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol
import xmltodict

from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class BluesoundConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bluesound Alt."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with no discovered player."""
        self._discovered: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the player's address and check that it answers."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            try:
                sync_status = await self._fetch_sync_status(host, port)
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except ExpatError:
                # Something answered, but not with XML: not a BluOS player.
                errors["base"] = "invalid_response"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                if sync_status is None:
                    errors["base"] = "invalid_response"
                else:
                    mac = sync_status.get("mac", "").replace(":", "").lower()
                    name = sync_status.get("name") or f"Bluesound {host}"

                    await self.async_set_unique_id(mac)
                    # Re-adding a player that moved updates its address.
                    self._abort_if_unique_id_configured(
                        updates={CONF_HOST: host, CONF_PORT: port}
                    )

                    return self.async_create_entry(
                        title=name,
                        data={CONF_HOST: host, CONF_PORT: port},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Offer a player that announced itself, or update a known one's address.

        BluOS players announce _musc._tcp. A player already set up is not
        offered again; its address is updated instead, so a new DHCP lease
        does not leave it unreachable.
        """
        host = next(
            (str(ip) for ip in discovery_info.ip_addresses if ip.version == 4), None
        )
        if host is None:
            # The players' API is reached over IPv4.
            return self.async_abort(reason="no_ipv4_address")
        port = discovery_info.port or DEFAULT_PORT

        try:
            sync_status = await self._fetch_sync_status(host, port)
        except (aiohttp.ClientError, TimeoutError, ExpatError):
            return self.async_abort(reason="cannot_connect")
        if sync_status is None or not sync_status["mac"]:
            return self.async_abort(reason="invalid_response")

        mac = sync_status["mac"].replace(":", "").lower()
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})

        name = sync_status["name"] or f"Bluesound {host}"
        self._discovered = {CONF_HOST: host, CONF_PORT: port, "name": name}
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before adding a discovered player."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered["name"],
                data={
                    CONF_HOST: self._discovered[CONF_HOST],
                    CONF_PORT: self._discovered[CONF_PORT],
                },
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._discovered["name"],
                "host": self._discovered[CONF_HOST],
            },
        )

    async def _fetch_sync_status(self, host: str, port: int) -> dict | None:
        session = async_get_clientsession(self.hass)
        url = f"http://{host}:{port}/SyncStatus"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()

        parsed = xmltodict.parse(text)
        sync = parsed.get("SyncStatus")
        if not sync:
            return None

        return {
            "mac": sync.get("@mac", ""),
            "name": sync.get("@name") or sync.get("name", ""),
        }
