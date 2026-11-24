"""Diagnostics. Tokens, accounts, keys, IDs and device serials are redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FilamentStockConfigEntry
from .const import CONF_ACCOUNT, CONF_FOLDER_ID, CONF_SERVICE_ACCOUNT, CONF_TOKEN

TO_REDACT = {
    CONF_ACCOUNT,
    CONF_TOKEN,
    CONF_SERVICE_ACCOUNT,
    CONF_FOLDER_ID,
    # Cloud spool record fields that identify a person or a device.
    "RFID",
    "rfid",
    "devId",
    "amsSn",
    "deviceName",
    "uid",
    "userId",
    "email",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FilamentStockConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for one entry."""
    data = entry.runtime_data
    spools = data.spools.data
    queue = data.queue.data
    tracker = data.tracker
    return {
        "tracker": {
            "printers": [sorted(keys) for keys in tracker.printers.values()],
            "jobs": [
                {k: v for k, v in asdict(job).items() if k != "serial"}
                for job in tracker.jobs.values()
            ],
            "last": tracker.last,
        },
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "spools": {
            "last_update_success": data.spools.last_update_success,
            "count": len(spools.spools),
            "groups": [
                {
                    "id": g.id,
                    "material": g.material,
                    "color": g.color,
                    "note": g.note,
                    "spools": [s.id for s in g.spools],
                }
                for g in spools.groups.values()
            ],
            # Raw cloud records: needed to confirm color, status and note formats.
            "raw": async_redact_data(spools.raw, TO_REDACT),
        },
        "queue": {
            "last_update_success": data.queue.last_update_success,
            "files": len(queue.files),
            "plates": queue.plates,
            "not_sliced": len(queue.not_sliced),
            "pending": len(queue.pending),
            "statuses": sorted(
                f.parsed.status.value if f.parsed else "pending"
                for f in queue.files.values()
            ),
        },
    }
