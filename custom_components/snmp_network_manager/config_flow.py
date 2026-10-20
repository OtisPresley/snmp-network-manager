import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN

class SnmpNetworkManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SNMP Network Manager."""
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return SnmpNetworkManagerOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="SNMP Network Manager", data={})

        return self.async_show_form(step_id="user")


class SnmpNetworkManagerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for SNMP Network Manager."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options

        # Default values
        scan_interval = options.get("scan_interval", 60)
        stale_days = options.get("stale_days", 30)
        history_days = options.get("history_days", 30)
        exclude_macs = options.get("exclude_macs", "")
        exclude_ips = options.get("exclude_ips", "")
        exclude_names = options.get("exclude_names", "")

        schema = vol.Schema({
            vol.Required("scan_interval", default=scan_interval): vol.Coerce(int),
            vol.Required("stale_days", default=stale_days): vol.Coerce(int),
            vol.Required("history_days", default=history_days): vol.Coerce(int),
            vol.Optional("exclude_macs", default=exclude_macs): str,
            vol.Optional("exclude_ips", default=exclude_ips): str,
            vol.Optional("exclude_names", default=exclude_names): str,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema
        )
