"""Filament Stock: filament stock, print queue needs and automatic deduction."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_SERVICE_ACCOUNT, DOMAIN
from .drive import DriveClient, DriveError
from .print_queue import QueueCoordinator


@dataclass(slots=True)
class FilamentStockData:
    """Runtime objects for one config entry."""

    queue: QueueCoordinator


type FilamentStockConfigEntry = ConfigEntry[FilamentStockData]


async def async_setup_entry(
    hass: HomeAssistant, entry: FilamentStockConfigEntry
) -> bool:
    """Set up from a config entry. Does not wait on Drive downloads."""
    try:
        client = DriveClient(
            async_get_clientsession(hass), entry.data[CONF_SERVICE_ACCOUNT]
        )
    except DriveError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="invalid_service_account"
        ) from err

    queue = QueueCoordinator(hass, entry, client)
    await queue.async_load_cache()
    entry.runtime_data = FilamentStockData(queue=queue)

    entry.async_create_background_task(
        hass, queue.async_refresh(), f"{DOMAIN} queue refresh"
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: FilamentStockConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: FilamentStockConfigEntry
) -> bool:
    """Unload a config entry."""
    return True
