from homeassistant.helpers.selector import selector
from homeassistant import config_entries
import voluptuous as vol


from .const import DOMAIN

class BluesoundConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bluesound."""

    def __init__(self):
        """Initialize a new AppleTVConfigFlow."""
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