"""Sensors: needed and balance per color group, plus queue totals."""

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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FilamentStockConfigEntry
from .entity import FilamentStockEntity, GroupEntity, async_track_groups
from .stock import StockManager
from .tracker import PrintTracker

PARALLEL_UPDATES = 0


SENSOR_KINDS = ("needed", "balance")


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
            LastDeductionSensor(manager, entry, entry.runtime_data.tracker),
        ]
    )

    # Stock is a number entity since 0.1.0: drop stock sensors of earlier builds.
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "sensor" and reg_entry.translation_key == "stock":
            registry.async_remove(reg_entry.entity_id)

    entry.async_on_unload(
        async_track_groups(
            hass,
            entry,
            manager,
            domain="sensor",
            kinds=SENSOR_KINDS,
            add=async_add_entities,
            factory=lambda group_id, kind: GroupSensor(manager, entry, group_id, kind),
        )
    )


class GroupSensor(GroupEntity, SensorEntity):
    """Needed or balance of one color group, in grams."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self, manager: StockManager, entry: ConfigEntry, group_id: str, kind: str
    ) -> None:
        super().__init__(manager, entry, group_id, kind)
        self._attr_icon = {"needed": "mdi:tray-full", "balance": "mdi:scale-balance"}[
            kind
        ]

    @property
    def native_value(self) -> float | None:
        item = self._item
        if item is None:
            return None
        return round(item.needed if self.kind == "needed" else item.balance, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = self._base_attributes()
        if attrs is None or self._item is None:
            return attrs
        if self.kind == "balance":
            attrs["stock"] = round(self._item.stock, 1)
            attrs["spools"] = self._item.spools
        attrs["file_colors"] = self._item.sources
        attrs["files"] = sorted({f for s in self._item.sources for f in s["files"]})
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


LAST_RESULTS = [
    "none",
    "booked",
    "duplicate",
    "no_match",
    "stopped",
    "missed_end",
    "no_printer",
]


class LastDeductionSensor(FilamentStockEntity, SensorEntity):
    """Outcome of the last tracked print: what was booked, or why not."""

    _attr_translation_key = "last_deduction"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LAST_RESULTS
    _attr_icon = "mdi:history"

    def __init__(
        self, manager: StockManager, entry: ConfigEntry, tracker: PrintTracker
    ) -> None:
        super().__init__(manager, entry, "last_deduction")
        self._tracker = tracker

    @property
    def native_value(self) -> str:
        result = self._tracker.last.get("result", "none")
        return result if result in LAST_RESULTS else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {k: v for k, v in self._tracker.last.items() if k != "result"}
        attrs["printing"] = bool(self._tracker.jobs)
        return attrs
