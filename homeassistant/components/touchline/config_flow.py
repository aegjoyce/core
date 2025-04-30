import logging
from typing import Any

import voluptuous as vol
from pytouchline_extended import PyTouchline

from homeassistant import config_entries
from homeassistant.const import CONF_HOST

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class RothTouchlineConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Roth Touchline New."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            # Automatically prepend http:// if no scheme is provided.
            if not host.startswith(("http://", "https://")):
                host = "http://" + host

            try:
                # Create a PyTouchline instance.
                touchline = PyTouchline(url=host)
                # Offload the blocking call to the executor.
                number_of_devices = await self.hass.async_add_executor_job(
                    touchline.get_number_of_devices
                )
                number_of_devices = int(number_of_devices)
                _LOGGER.debug(
                    "Found %s devices on Touchline hub %s", number_of_devices, host
                )
            except Exception as ex:
                _LOGGER.error("Error connecting to Touchline hub at %s: %s", host, ex)
                errors["base"] = "cannot_connect"
            else:
                # Return a successful entry once validated.
                return self.async_create_entry(
                    title=f"Touchline Hub ({host})",
                    data={CONF_HOST: host},
                )

        schema = vol.Schema({vol.Required(CONF_HOST): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
