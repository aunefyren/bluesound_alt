from homeassistant.helpers.selector import selector

class BluesoundConfigFlow(data_entry_flow.FlowHandler):
    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("Name"): str
                },
                {
                    vol.Required("IP"): str
                }
            ),
            errors=errors,
        )
    
    async def async_create_player(self, user_input=None):
        return self.async_create_entry(
            title="Title of the entry",
            data={
                "username": user_input["username"],
                "password": user_input["password"]
            },
            options={
                "mobile_number": user_input["mobile_number"]
            },
        )
    
    async def create_player_object()
    player = BluesoundPlayer(HomeAssistant, host, port, name, _add_player_cb)