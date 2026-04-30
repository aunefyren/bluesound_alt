"""Config flow for Bluesound Alt."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
import xmltodict

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            try:
                sync_status = await self._fetch_sync_status(host, port)
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
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
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=name,
                        data={CONF_HOST: host, CONF_PORT: port},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
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
