import voluptuous as vol
from homeassistant.helpers.selector import selector
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaFlowFormStep,
    SchemaOptionsFlowHandler,
)

OPTIONS_SCHEMA = vol.Schema({})
OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(OPTIONS_SCHEMA),
}



from .const import DOMAIN

class BluesoundConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bluesound."""

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SchemaOptionsFlowHandler:
        """Get options flow for this handler."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)

    def __init__(self):
        """Initialize a new ConfigFlow."""
        self.ip = None
        self.name = None

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("Name"): str
                },
                {
                    vol.Required("IP"): str
                }
            ),
            errors=errors
        )