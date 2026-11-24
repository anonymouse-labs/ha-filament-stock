"""Sensors: stock, needed and balance per color group, plus queue totals."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FilamentStockConfigEntry
from .const import DOMAIN, GROUP_KINDS
from .entity import FilamentStockEntity
from .stock import GroupState, StockManager

PARALLEL_UPDATES = 0


def group_unique_id(entry_id: str, group_id: str, kind: str) -> str:
    return f"{entry_id}_{group_id}_{kind}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FilamentStockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    manager = entry.runtime_data.manager
    async_add_entities(
        [
            QueuePlatesSensor(manager, entry),
            TotalNeededSensor(manager, entry),
            TotalStockSensor(manager, entry),
            NotSlicedSensor(manager, entry),
            UnmatchedSensor(manager, entry),
        ]
    )

    registry = er.async_get(hass)
    known: set[str] = set()
    prefix = f"{entry.entry_id}_"
    # Groups from an earlier run: removed below if they vanished meanwhile.
    stored: set[str] = set()
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        # translation_key tells group sensors apart from e.g. 'total_needed'.
        kind = reg_entry.translation_key
        suffix = f"_{kind}"
        if kind in GROUP_KINDS and reg_entry.unique_id.endswith(suffix):
            stored.add(reg_entry.unique_id.removeprefix(prefix).removesuffix(suffix))

    @callback
    def _sync_groups() -> None:
        current = set(manager.state)
        new = current - known
        if new:
            async_add_entities(
                GroupSensor(manager, entry, group_id, kind)
                for group_id in sorted(new)
                for kind in GROUP_KINDS
            )
        if not manager.ready:
            # Data may be incomplete: keep every entity until both sources load.
            known.update(current)
            return
        # A vanished group is removed from the registry, never shown as 0 g.
        for group_id in (known | stored) - current:
            for kind in GROUP_KINDS:
                uid = group_unique_id(entry.entry_id, group_id, kind)
                if entity_id := registry.async_get_entity_id("sensor", DOMAIN, uid):
                    registry.async_remove(entity_id)
        stored.clear()
        known.clear()
        known.update(current)

    _sync_groups()
    entry.async_on_unload(manager.add_listener(_sync_groups))


class GroupSensor(FilamentStockEntity, SensorEntity):
    """Stock, needed or balance of one color group, in grams."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self, manager: StockManager, entry: ConfigEntry, group_id: str, kind: str
    ) -> None:
        super().__init__(manager, entry, f"{group_id}_{kind}")
        self.group_id = group_id
        self.kind = kind
        self._attr_translation_key = kind
        item = manager.state[group_id]
        self._attr_translation_placeholders = {"group": item.label}
        self._attr_icon = {
            "stock": "mdi:printer-3d-nozzle",
            "needed": "mdi:tray-full",
            "balance": "mdi:scale-balance",
        }[kind]

    @property
    def _item(self) -> GroupState | None:
        return self.manager.state.get(self.group_id)

    @property
    def available(self) -> bool:
        return self._item is not None

    @property
    def native_value(self) -> float | None:
        item = self._item
        if item is None:
            return None
        value = {"stock": item.stock, "needed": item.needed, "balance": item.balance}
        return round(value[self.kind], 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        item = self._item
        if item is None:
            return None
        attrs: dict[str, Any] = {
            "material": item.material,
            "color": item.color,
            "note": item.note,
        }
        if item.unknown:
            attrs["unknown"] = True
        if self.kind in ("stock", "balance"):
            attrs["spools"] = item.spools
        if self.kind in ("needed", "balance"):
            attrs["file_colors"] = item.sources
        return attrs


@dataclass(frozen=True, slots=True)
class _Fixed:
    key: str
    icon: str
    value: Callable[[StockManager], float | int]
    attrs: Callable[[StockManager], dict[str, Any]] | None = None
    grams: bool = False
    diagnostic: bool = False


class _FixedSensor(FilamentStockEntity, SensorEntity):
    spec: _Fixed

    def __init__(self, manager: StockManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, self.spec.key)
        self._attr_translation_key = self.spec.key
        self._attr_icon = self.spec.icon
        if self.spec.grams:
            self._attr_device_class = SensorDeviceClass.WEIGHT
            self._attr_native_unit_of_measurement = UnitOfMass.GRAMS
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 0
        if self.spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | int:
        value = self.spec.value(self.manager)
        return round(value, 1) if isinstance(value, float) else value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.spec.attrs(self.manager) if self.spec.attrs else None


def _queue_attrs(manager: StockManager) -> dict[str, Any]:
    queue = manager.queue.data
    return {
        "folder": queue.folder_name,
        "files": [
            {
                "name": f.name,
                "plates": len(f.parsed.plates),
                "grams": round(sum(f.parsed.usage().values()), 1),
            }
            for f in sorted(queue.sliced, key=lambda f: f.name)
            if f.parsed
        ],
        "pending": queue.pending,
        "other_files": queue.other_files,
    }


class QueuePlatesSensor(_FixedSensor):
    spec = _Fixed(
        "queue_plates",
        "mdi:layers-triple",
        lambda m: m.queue.data.plates,
        _queue_attrs,
    )


class TotalNeededSensor(_FixedSensor):
    spec = _Fixed("total_needed", "mdi:tray-full", lambda m: m.total_needed, grams=True)


class TotalStockSensor(_FixedSensor):
    spec = _Fixed(
        "total_stock", "mdi:printer-3d-nozzle", lambda m: m.total_stock, grams=True
    )


class NotSlicedSensor(_FixedSensor):
    spec = _Fixed(
        "not_sliced",
        "mdi:file-alert-outline",
        lambda m: len(m.queue.data.not_sliced),
        lambda m: {"files": m.queue.data.not_sliced},
        diagnostic=True,
    )


class UnmatchedSensor(_FixedSensor):
    spec = _Fixed(
        "unmatched_colors",
        "mdi:palette-outline",
        lambda m: len(m.needs.unmatched),
        lambda m: {
            "colors": [
                {"material": x.material, "color": x.color} for x in m.needs.unmatched
            ]
        },
        diagnostic=True,
    )
