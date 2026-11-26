"""Pick the color group for a color used in the sliced files."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FilamentStockConfigEntry
from .entity import FilamentStockEntity, async_track_groups
from .matcher import fixed_key
from .stock import FileColor, StockManager

PARALLEL_UPDATES = 0

AUTO = "auto"
KIND = "file_color"


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
            domain="select",
            kinds=(KIND,),
            add=async_add_entities,
            factory=lambda key, kind: FileColorSelect(manager, entry, key),
            keys=lambda: set(manager.file_colors),
        )
    )


class FileColorSelect(FilamentStockEntity, SelectEntity):
    """'Automatic', or a fixed color group, for one file color."""

    _attr_translation_key = KIND
    _attr_icon = "mdi:palette-swatch-variant"

    def __init__(self, manager: StockManager, entry: ConfigEntry, key: str) -> None:
        super().__init__(manager, entry, f"{key}_{KIND}")
        self.key = key
        self._attr_translation_placeholders = {"color": manager.file_colors[key].label}

    @property
    def _color(self) -> FileColor | None:
        return self.manager.file_colors.get(self.key)

    @property
    def available(self) -> bool:
        return self._color is not None

    @property
    def options(self) -> list[str]:
        return [AUTO, *self.manager.group_choices()]

    @property
    def current_option(self) -> str | None:
        color = self._color
        if color is None:
            return None
        target = self.manager.fixed.get(
            fixed_key(color.match.material, color.match.color)
        )
        for label, group_id in self.manager.group_choices().items():
            if group_id == target:
                return label
        return AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        color = self._color
        if color is None:
            return None
        match = color.match
        attrs: dict[str, Any] = {
            "material": match.material,
            "color": match.color,
            "method": match.method.value,
            "counted_as": self._label(match.group_id) if match.matched else None,
            "files": color.files,
        }
        if match.distance is not None:
            attrs["distance"] = match.distance
        if len({m.group_id for m in color.matches.values()}) > 1:
            # Same color, other filament profile: counted on another group.
            attrs["by_filament_id"] = {
                fid or "-": self._label(m.group_id)
                for fid, m in sorted(color.matches.items())
            }
        if color.suggestion:
            attrs["nearest"] = self._label(color.suggestion[0])
            attrs["nearest_distance"] = color.suggestion[1]
        return attrs

    def _label(self, group_id: str) -> str:
        item = self.manager.state.get(group_id)
        return item.label if item else group_id

    async def async_select_option(self, option: str) -> None:
        color = self._color
        if color is None:
            return
        target = None if option == AUTO else self.manager.group_choices()[option]
        self.manager.set_fixed(color.match.material, color.match.color, target)
