"""Config flow for Filament Stock.

Order: Bambu Lab account (with email code or 2FA), then the Google Drive
folder. The password is only used to get a token and is never stored.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio

from .bambu_api import (
    BambuCloudClient,
    BambuError,
    CurlTransport,
    LoginResult,
    token_expiry,
    token_user,
)
from .const import (
    CONF_ACCOUNT,
    CONF_FOLDER_ID,
    CONF_QUEUE_INTERVAL,
    CONF_SERVICE_ACCOUNT,
    CONF_SPOOL_INTERVAL,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES,
    DEFAULT_QUEUE_INTERVAL,
    DEFAULT_SPOOL_INTERVAL,
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

CONF_CODE = "code"

_DRIVE_FIELD_ERRORS = {
    "invalid_folder_id": CONF_FOLDER_ID,
    "folder_not_found": CONF_FOLDER_ID,
    "not_a_folder": CONF_FOLDER_ID,
}

CODE_SCHEMA = probatio.Schema({probatio.Required(CONF_CODE): TextSelector()})


def _drive_schema(service_account_required: bool) -> probatio.Schema:
    marker = probatio.Required if service_account_required else probatio.Optional
    return probatio.Schema(
        {
            marker(CONF_SERVICE_ACCOUNT): TextSelector(
                TextSelectorConfig(multiline=True)
            ),
            probatio.Required(CONF_FOLDER_ID): TextSelector(),
        }
    )


class FilamentStockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Filament Stock."""

    VERSION = 1

    def __init__(self) -> None:
        self._account = ""
        self._tfa_key = ""
        self._token = ""

    async def _bambu(
        self, call: Callable[[BambuCloudClient], Awaitable[LoginResult]]
    ) -> tuple[LoginResult | None, dict[str, str]]:
        """Run one login call on a fresh client; map errors to form keys."""
        client = BambuCloudClient(CurlTransport())
        try:
            return await call(client), {}
        except BambuError as err:
            return None, {"base": err.key}
        finally:
            await client.close()

    # Bambu Lab account

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_account(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._account = str(entry_data.get(CONF_ACCOUNT, ""))
        return await self.async_step_account()

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bambu Lab email and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT].strip()
            password = user_input[CONF_PASSWORD]
            result, errors = await self._bambu(
                lambda c: c.login(self._account, password)
            )
            if result is not None:
                return await self._after_login(result)

        schema = probatio.Schema(
            {
                probatio.Required(CONF_ACCOUNT, default=self._account): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                ),
                probatio.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="account", data_schema=schema, errors=errors
        )

    async def _after_login(self, result: LoginResult) -> ConfigFlowResult:
        if result.token:
            self._token = result.token
            return await self._finish_account()
        if result.needs == "tfa" and result.tfa_key:
            self._tfa_key = result.tfa_key
            return await self.async_step_tfa()
        return await self.async_step_verify_code()

    async def async_step_verify_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Code that Bambu Lab emailed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_CODE]
            result, errors = await self._bambu(
                lambda c: c.login_with_code(self._account, code)
            )
            if result is not None:
                return await self._after_login(result)
        return self.async_show_form(
            step_id="verify_code", data_schema=CODE_SCHEMA, errors=errors
        )

    async def async_step_tfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Code from the authenticator app."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_CODE]
            result, errors = await self._bambu(
                lambda c: c.login_with_tfa(self._tfa_key, code)
            )
            if result is not None:
                return await self._after_login(result)
        return self.async_show_form(
            step_id="tfa", data_schema=CODE_SCHEMA, errors=errors
        )

    def _token_data(self) -> dict[str, Any]:
        return {
            CONF_ACCOUNT: self._account,
            CONF_TOKEN: self._token,
            CONF_TOKEN_EXPIRES: token_expiry(self._token),
        }

    async def _finish_account(self) -> ConfigFlowResult:
        # Bambu user name from the token; email only when the token has none.
        await self.async_set_unique_id(
            token_user(self._token) or self._account.casefold()
        )
        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                entry, data_updates=self._token_data()
            )
        self._abort_if_unique_id_configured()
        return await self.async_step_drive()

    # Google Drive folder

    async def async_step_drive(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Service account JSON and the shared folder."""
        reconfigure = self.source == SOURCE_RECONFIGURE
        errors: dict[str, str] = {}
        placeholders = {"setup_url": DRIVE_SETUP_URL, "email": "-"}
        if user_input is not None:
            service_account = (user_input.get(CONF_SERVICE_ACCOUNT) or "").strip()
            if reconfigure and not service_account:
                # Empty on reconfigure: keep the stored key.
                service_account = self._get_reconfigure_entry().data[
                    CONF_SERVICE_ACCOUNT
                ]
            try:
                client = DriveClient(
                    async_get_clientsession(self.hass), service_account
                )
                placeholders["email"] = client.email
                folder_id = parse_folder_id(user_input[CONF_FOLDER_ID])
                await client.get_folder(folder_id)
            except (DriveInvalidJson, DriveInvalidServiceAccount) as err:
                errors[CONF_SERVICE_ACCOUNT] = err.key
            except DriveError as err:
                errors[_DRIVE_FIELD_ERRORS.get(err.key, "base")] = err.key
            else:
                drive_data = {
                    CONF_SERVICE_ACCOUNT: service_account,
                    CONF_FOLDER_ID: folder_id,
                }
                if reconfigure:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data_updates=drive_data
                    )
                return self.async_create_entry(
                    title="Filament Stock", data={**self._token_data(), **drive_data}
                )

        suggested = dict(user_input or {})
        if reconfigure:
            # Never send the stored private key back to the browser.
            suggested.pop(CONF_SERVICE_ACCOUNT, None)
            suggested.setdefault(
                CONF_FOLDER_ID, self._get_reconfigure_entry().data.get(CONF_FOLDER_ID)
            )
        return self.async_show_form(
            step_id="drive",
            data_schema=self.add_suggested_values_to_schema(
                _drive_schema(not reconfigure), suggested
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    # Reconfigure: sign in again, or change the Drive folder

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._account = str(self._get_reconfigure_entry().data.get(CONF_ACCOUNT, ""))
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["account", "drive"]
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FilamentStockOptionsFlow()


def _minutes(maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=1,
            max=maximum,
            step=1,
            unit_of_measurement="min",
            mode=NumberSelectorMode.BOX,
        )
    )


class FilamentStockOptionsFlow(OptionsFlowWithReload):
    """Polling intervals."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_SPOOL_INTERVAL: int(user_input[CONF_SPOOL_INTERVAL]),
                    CONF_QUEUE_INTERVAL: int(user_input[CONF_QUEUE_INTERVAL]),
                }
            )
        options = self.config_entry.options
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_SPOOL_INTERVAL,
                    default=options.get(CONF_SPOOL_INTERVAL, DEFAULT_SPOOL_INTERVAL),
                ): _minutes(1440),
                probatio.Required(
                    CONF_QUEUE_INTERVAL,
                    default=options.get(CONF_QUEUE_INTERVAL, DEFAULT_QUEUE_INTERVAL),
                ): _minutes(1440),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
