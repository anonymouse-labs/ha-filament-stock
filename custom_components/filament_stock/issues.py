"""Repair issues for unsliced files and file colors without a color group."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .stock import StockManager

MAX_LISTED = 10


def _issue(
    hass: HomeAssistant, issue_id: str, key: str, items: list[str], label: str
) -> None:
    if not items:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    shown = ", ".join(items[:MAX_LISTED])
    if len(items) > MAX_LISTED:
        shown += f" (+{len(items) - MAX_LISTED})"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders={label: shown},
    )


@callback
def async_track_issues(
    hass: HomeAssistant, entry: ConfigEntry, manager: StockManager
) -> Callable[[], None]:
    """Keep the issues in step with the stock manager."""

    @callback
    def _update() -> None:
        if not manager.ready:
            return
        queue = manager.queue.data
        _issue(
            hass,
            f"not_sliced_{entry.entry_id}",
            "not_sliced",
            queue.not_sliced if queue else [],
            "files",
        )
        _issue(
            hass,
            f"unmatched_{entry.entry_id}",
            "unmatched_colors",
            # One color can come with several filament profiles: list it once.
            list(
                dict.fromkeys(
                    f"{m.material} {m.color}" for m in manager.needs.unmatched
                )
            ),
            "colors",
        )

    _update()
    remove = manager.add_listener(_update)

    @callback
    def _unload() -> None:
        remove()
        for prefix in ("not_sliced", "unmatched"):
            ir.async_delete_issue(hass, DOMAIN, f"{prefix}_{entry.entry_id}")

    return _unload
