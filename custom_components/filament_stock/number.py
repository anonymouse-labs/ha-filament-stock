"""Stock per color group, editable in Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FilamentStockConfigEntry
from .entity import GroupEntity, async_track_groups
from .stock import StockManager

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FilamentStockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    manager = entry.runtime_data.manager
    entry.async_on_unload(
        async_track_groups(
            hass,
            entry,
            manager,
            domain="number",
            kinds=("stock",),
            add=async_add_entities,
            factory=lambda group_id, kind: StockNumber(manager, entry, group_id, kind),
        )
    )


class StockNumber(GroupEntity, NumberEntity):
    """Grams in stock for a color group.

    Starts from the cloud weight minus local usage. A value entered here is a
    local correction for the group, kept until the cloud weight changes.
    """

    _attr_device_class = NumberDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_native_min_value = 0
    _attr_native_max_value = 100_000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:warehouse"

    def __init__(
        self, manager: StockManager, entry: ConfigEntry, group_id: str, kind: str
    ) -> None:
        super().__init__(manager, entry, group_id, kind)

    @property
    def native_value(self) -> float | None:
        item = self._item
        return None if item is None else round(item.stock, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = self._base_attributes()
        if attrs is not None and self._item is not None:
            attrs["spools"] = self._item.spools
            attrs["correction"] = round(
                -self.manager.ledger.shortage.get(self.group_id, 0.0), 1
            )
        return attrs

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_group_stock(self.group_id, value)
