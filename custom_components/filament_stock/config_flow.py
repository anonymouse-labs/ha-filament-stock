"""Config flow for Filament Stock."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
import probatio

from .const import (
    CONF_FOLDER_ID,
    CONF_QUEUE_INTERVAL,
    CONF_SERVICE_ACCOUNT,
    DEFAULT_QUEUE_INTERVAL,
    DOMAIN,
    DRIVE_SETUP_URL,
)
from .drive import (
    DriveClient,
    DriveError,
    DriveInvalidJson,
    DriveInvalidServiceAccount,
    parse_folder_id,
)

DRIVE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SERVICE_ACCOUNT): TextSelector(
            TextSelectorConfig(multiline=True)
        ),
        probatio.Required(CONF_FOLDER_ID): TextSelector(),
    }
)

_FIELD_ERRORS = {
    "invalid_folder_id": CONF_FOLDER_ID,
    "folder_not_found": CONF_FOLDER_ID,
    "not_a_folder": CONF_FOLDER_ID,
}


class FilamentStockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Filament Stock."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_drive(user_input)

    async def async_step_drive(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Service account JSON and the shared folder."""
        errors: dict[str, str] = {}
        placeholders = {"setup_url": DRIVE_SETUP_URL, "email": "-"}
        if user_input is not None:
            try:
                client = DriveClient(
                    async_get_clientsession(self.hass), user_input[CONF_SERVICE_ACCOUNT]
                )
                placeholders["email"] = client.email
                folder_id = parse_folder_id(user_input[CONF_FOLDER_ID])
                await client.get_folder(folder_id)
            except (DriveInvalidJson, DriveInvalidServiceAccount) as err:
                errors[CONF_SERVICE_ACCOUNT] = err.key
            except DriveError as err:
                errors[_FIELD_ERRORS.get(err.key, "base")] = err.key
            else:
                await self.async_set_unique_id(folder_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Filament Stock",
                    data={
                        CONF_SERVICE_ACCOUNT: user_input[CONF_SERVICE_ACCOUNT].strip(),
                        CONF_FOLDER_ID: folder_id,
                    },
                )

        return self.async_show_form(
            step_id="drive",
            data_schema=self.add_suggested_values_to_schema(
                DRIVE_SCHEMA, user_input or {}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FilamentStockOptionsFlow()


class FilamentStockOptionsFlow(OptionsFlow):
    """Polling intervals."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_QUEUE_INTERVAL: int(user_input[CONF_QUEUE_INTERVAL]),
                }
            )
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_QUEUE_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_QUEUE_INTERVAL, DEFAULT_QUEUE_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=1440,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
