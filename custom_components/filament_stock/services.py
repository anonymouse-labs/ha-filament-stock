"""Actions: refresh, map a file color to a group, reset the local ledger."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
import probatio

from .colors import normalize_hex
from .const import DOMAIN, GROUP_KINDS
from .stock import StockManager

SERVICE_REFRESH = "refresh"
SERVICE_MAP_COLOR = "map_color"
SERVICE_RESET_LEDGER = "reset_ledger"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MATERIAL = "material"
ATTR_COLOR = "color"
ATTR_GROUP = "group"
ATTR_SPOOL_ID = "spool_id"

REFRESH_SCHEMA = probatio.Schema({probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
MAP_COLOR_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_MATERIAL): cv.string,
        probatio.Required(ATTR_COLOR): cv.string,
        probatio.Optional(ATTR_GROUP): cv.entity_id,
    }
)
RESET_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_GROUP): cv.entity_id,
        probatio.Optional(ATTR_SPOOL_ID): cv.positive_int,
    }
)


def _managers(hass: HomeAssistant) -> dict[str, StockManager]:
    return {
        entry.entry_id: entry.runtime_data.manager
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    }


def _group_of(hass: HomeAssistant, entity_id: str) -> tuple[StockManager, str]:
    """Resolve a group sensor entity to its manager and group id."""
    entity = er.async_get(hass).async_get(entity_id)
    manager = _managers(hass).get(entity.config_entry_id or "") if entity else None
    if entity is None or manager is None or entity.platform != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_group",
            translation_placeholders={ATTR_ENTITY_ID: entity_id},
        )
    group_id, _, kind = entity.unique_id.removeprefix(
        f"{entity.config_entry_id}_"
    ).rpartition("_")
    if kind not in GROUP_KINDS or group_id not in manager.state:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_group",
            translation_placeholders={ATTR_ENTITY_ID: entity_id},
        )
    return manager, group_id


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the domain."""

    async def refresh(call: ServiceCall) -> None:
        managers = _managers(hass)
        wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if wanted is not None:
            if wanted not in managers:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="entry_not_loaded"
                )
            managers = {wanted: managers[wanted]}
        for manager in managers.values():
            await manager.spools.async_refresh()
            await manager.queue.async_refresh()

    async def map_color(call: ServiceCall) -> None:
        color = normalize_hex(call.data[ATTR_COLOR])
        if color is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_color"
            )
        material = call.data[ATTR_MATERIAL]
        if ATTR_GROUP in call.data:
            manager, group_id = _group_of(hass, call.data[ATTR_GROUP])
            if manager.state[group_id].unknown:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="unknown_group_target"
                )
            manager.set_fixed(material, color, group_id)
            return
        for manager in _managers(hass).values():
            manager.set_fixed(material, color, None)

    async def reset_ledger(call: ServiceCall) -> None:
        manager, group_id = _group_of(hass, call.data[ATTR_GROUP])
        if not manager.reset_group(group_id, call.data.get(ATTR_SPOOL_ID)):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="spool_not_in_group"
            )

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh, REFRESH_SCHEMA)
    async_register_admin_service(
        hass, DOMAIN, SERVICE_MAP_COLOR, map_color, MAP_COLOR_SCHEMA
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_RESET_LEDGER, reset_ledger, RESET_SCHEMA
    )
