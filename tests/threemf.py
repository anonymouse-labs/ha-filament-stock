"""Build synthetic .gcode.3mf files in memory for tests."""

from __future__ import annotations

import io
import zipfile

SLICE_INFO = "Metadata/slice_info.config"


def slice_info(plates: list[list[tuple]], index: bool = True) -> str:
    """slice_info.config XML. Each plate is a list of (type, color, grams) or
    (type, color, grams, filament profile id)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for number, filaments in enumerate(plates, start=1):
        parts.append("  <plate>")
        if index:
            parts.append(f'    <metadata key="index" value="{number}"/>')
        weight = round(sum(f[2] for f in filaments), 2)
        parts.append(f'    <metadata key="weight" value="{weight}"/>')
        for slot, (kind, color, grams, *profile) in enumerate(filaments, start=1):
            idx = f' tray_info_idx="{profile[0]}"' if profile else ""
            parts.append(
                f'    <filament id="{slot}"{idx} type="{kind}" color="{color}" '
                f'used_m="1.00" used_g="{grams}"/>'
            )
        parts.append("  </plate>")
    parts.append("</config>")
    return "\n".join(parts)


def build_3mf(
    config: str | bytes | None, extra: dict[str, bytes] | None = None
) -> bytes:
    """Zip with an optional slice_info.config and extra entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        if config is not None:
            archive.writestr(SLICE_INFO, config)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


# The user's reference plate: PLA #101410 15.02 g and PLA #898989 11.12 g.
REFERENCE_PLATE = [("PLA", "#101410", 15.02), ("PLA", "#898989", 11.12)]
