"""Base entities and the shared add/remove logic for color group entities."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, REPO_URL
from .stock import GroupState, StockManager


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


class GroupEntity(FilamentStockEntity):
    """One value of one color group."""

    def __init__(
        self, manager: StockManager, entry: ConfigEntry, group_id: str, kind: str
    ) -> None:
        super().__init__(manager, entry, f"{group_id}_{kind}")
        self.group_id = group_id
        self.kind = kind
        self._attr_translation_key = kind
        self._attr_translation_placeholders = {"group": manager.state[group_id].label}

    @property
    def _item(self) -> GroupState | None:
        return self.manager.state.get(self.group_id)

    @property
    def available(self) -> bool:
        return self._item is not None

    def _base_attributes(self) -> dict[str, Any] | None:
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
        return attrs


@callback
def async_track_groups(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manager: StockManager,
    *,
    domain: str,
    kinds: tuple[str, ...],
    add: Callable[[Iterable[Entity]], None],
    factory: Callable[[str, str], Entity],
    keys: Callable[[], set[str]] | None = None,
) -> Callable[[], None]:
    """Add entities for new keys (groups by default); remove vanished ones."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    known: set[str] = set()
    # Groups from an earlier run: removed below if they vanished meanwhile.
    stored: set[str] = set()
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        # translation_key tells group entities apart from e.g. 'total_needed'.
        kind = reg_entry.translation_key or ""
        if reg_entry.domain == domain and kind in kinds:
            stored.add(
                reg_entry.unique_id.removeprefix(prefix).removesuffix(f"_{kind}")
            )

    @callback
    def _sync() -> None:
        current = keys() if keys else set(manager.state)
        if new := current - known:
            add(factory(group_id, kind) for group_id in sorted(new) for kind in kinds)
        if not manager.ready:
            # Data may be incomplete: keep every entity until both sources load.
            known.update(current)
            return
        # A vanished group is removed from the registry, never shown as 0 g.
        for group_id in (known | stored) - current:
            for kind in kinds:
                uid = f"{prefix}{group_id}_{kind}"
                if entity_id := registry.async_get_entity_id(domain, DOMAIN, uid):
                    registry.async_remove(entity_id)
        stored.clear()
        known.clear()
        known.update(current)

    _sync()
    return manager.add_listener(_sync)
