"""Filament Stock: filament stock, print queue needs and automatic deduction."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bambu_api import BambuCloudClient, CurlTransport
from .const import CONF_SERVICE_ACCOUNT, CONF_TOKEN, DOMAIN
from .drive import DriveClient, DriveError
from .print_queue import QueueCoordinator
from .spools import SpoolCoordinator


@dataclass(slots=True)
class FilamentStockData:
    """Runtime objects for one config entry."""

    spools: SpoolCoordinator
    queue: QueueCoordinator


type FilamentStockConfigEntry = ConfigEntry[FilamentStockData]


async def async_setup_entry(
    hass: HomeAssistant, entry: FilamentStockConfigEntry
) -> bool:
    """Set up from a config entry. Never waits on the network."""
    try:
        drive = DriveClient(
            async_get_clientsession(hass), entry.data[CONF_SERVICE_ACCOUNT]
        )
    except DriveError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="invalid_service_account"
        ) from err

    bambu = BambuCloudClient(CurlTransport(), entry.data[CONF_TOKEN])
    entry.async_on_unload(bambu.close)

    spools = SpoolCoordinator(hass, entry, bambu)
    queue = QueueCoordinator(hass, entry, drive)
    await spools.async_load_cache()
    await queue.async_load_cache()
    spools.check_token_expiry()
    entry.runtime_data = FilamentStockData(spools=spools, queue=queue)

    entry.async_create_background_task(
        hass, spools.async_refresh(), f"{DOMAIN} spool refresh"
    )
    entry.async_create_background_task(
        hass, queue.async_refresh(), f"{DOMAIN} queue refresh"
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: FilamentStockConfigEntry
) -> bool:
    """Unload a config entry."""
    return True
