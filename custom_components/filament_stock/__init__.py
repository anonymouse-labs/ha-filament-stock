"""Filament Stock: filament stock, print queue needs and automatic deduction."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .bambu_api import BambuCloudClient, CurlTransport
from .const import CONF_SERVICE_ACCOUNT, CONF_TOKEN, DOMAIN
from .drive import DriveClient, DriveError
from .print_queue import QueueCoordinator
from .services import async_setup_services
from .spools import SpoolCoordinator
from .stock import StockManager

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class FilamentStockData:
    """Runtime objects for one config entry."""

    spools: SpoolCoordinator
    queue: QueueCoordinator
    manager: StockManager


type FilamentStockConfigEntry = ConfigEntry[FilamentStockData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions."""
    async_setup_services(hass)
    return True


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

    manager = StockManager(hass, entry, spools, queue)
    await manager.async_load()
    entry.runtime_data = FilamentStockData(spools=spools, queue=queue, manager=manager)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for unsub in manager.async_start():
        entry.async_on_unload(unsub)

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
    data = entry.runtime_data
    await data.manager.async_flush()
    await data.spools.async_flush()
    await data.queue.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
