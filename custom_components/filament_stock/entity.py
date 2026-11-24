"""Base entity for Filament Stock."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, REPO_URL
from .stock import StockManager


class FilamentStockEntity(Entity):
    """Entity fed by the stock manager. Shows the last known numbers."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: StockManager, entry: ConfigEntry, key: str) -> None:
        self.manager = manager
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Filament Stock",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=REPO_URL,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.manager.add_listener(self.async_write_ha_state))
