"""Parse filament usage from a sliced Bambu Studio plate file (*.gcode.3mf).

Pure Python, no Home Assistant imports. File content is untrusted: the zip is
read in memory only, sizes are bounded and XML with a DTD is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import io
import math
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

from .colors import normalize_hex

SLICE_INFO = "Metadata/slice_info.config"

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_SLICE_INFO_BYTES = 2 * 1024 * 1024
MAX_PLATES = 200
MAX_FILAMENTS_PER_PLATE = 64
# Bump when parsing changes: cached files are then downloaded and parsed again.
PARSE_VERSION = 2

UsageKey = tuple[str, str, str]  # (material, '#RRGGBB', filament id or '')


class ParseStatus(StrEnum):
    """Outcome of parsing one file."""

    OK = "ok"
    NOT_SLICED = "not_sliced"
    INVALID = "invalid"
    TOO_LARGE = "too_large"


@dataclass(frozen=True, slots=True)
class FilamentUse:
    """Filament used on one plate for one project filament slot."""

    slot: int
    material: str
    color: str  # '#RRGGBB'
    grams: float
    filament_id: str = ""  # slicer profile id, e.g. 'GFA01'; tells same colors apart


@dataclass(slots=True)
class Plate:
    """One sliced plate."""

    index: int
    filaments: list[FilamentUse] = field(default_factory=list)
    weight: float | None = None


@dataclass(slots=True)
class ParsedFile:
    """Result of parsing one file."""

    status: ParseStatus
    plates: list[Plate] = field(default_factory=list)

    @property
    def sliced(self) -> bool:
        return self.status is ParseStatus.OK

    def plate(self, index: int) -> Plate | None:
        return next((p for p in self.plates if p.index == index), None)

    def usage(self, plate_index: int | None = None) -> dict[UsageKey, float]:
        """Grams per (material, color, filament id) for one plate, or all plates."""
        plates = self.plates if plate_index is None else [self.plate(plate_index)]
        totals: dict[UsageKey, float] = {}
        for plate in plates:
            if plate is None:
                continue
            for use in plate.filaments:
                key = (use.material, use.color, use.filament_id)
                totals[key] = totals.get(key, 0.0) + use.grams
        return totals

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": PARSE_VERSION,
            "status": self.status.value,
            "plates": [
                {
                    "index": p.index,
                    "weight": p.weight,
                    "filaments": [
                        [f.slot, f.material, f.color, f.grams, f.filament_id]
                        for f in p.filaments
                    ],
                }
                for p in self.plates
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsedFile:
        return cls(
            status=ParseStatus(data["status"]),
            plates=[
                Plate(
                    index=int(p["index"]),
                    weight=p.get("weight"),
                    filaments=[
                        FilamentUse(int(f[0]), str(f[1]), str(f[2]), float(f[3]))
                        if len(f) == 4  # stored before PARSE_VERSION 2
                        else FilamentUse(
                            int(f[0]), str(f[1]), str(f[2]), float(f[3]), str(f[4])
                        )
                        for f in p["filaments"]
                    ],
                )
                for p in data.get("plates", [])
            ],
        )


_NAME_SUFFIXES = (".gcode.3mf", ".3mf", ".gcode")


def normalize_name(name: str) -> str:
    """File name for matching: base name, lower case, no suffix, only alnum."""
    value = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for suffix in _NAME_SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return "".join(ch for ch in value if ch.isalnum())


def normalize_material(value: str | None) -> str:
    """Material name as used for matching, e.g. 'PLA'."""
    return (value or "").strip().upper() or "UNKNOWN"


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _read_slice_info(data: bytes) -> bytes | ParseStatus:
    if len(data) > MAX_ARCHIVE_BYTES:
        return ParseStatus.TOO_LARGE
    # Untrusted input: zipfile can raise BadZipFile, zlib.error, RuntimeError
    # (encrypted), NotImplementedError and more. Any failure means invalid.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return ParseStatus.TOO_LARGE
            info = next((i for i in infos if i.filename == SLICE_INFO), None)
            if info is None:
                return ParseStatus.NOT_SLICED
            if info.file_size > MAX_SLICE_INFO_BYTES:
                return ParseStatus.TOO_LARGE
            with archive.open(info) as handle:
                # Bounded read: the header size can lie (zip bomb).
                raw = handle.read(MAX_SLICE_INFO_BYTES + 1)
    except Exception:  # noqa: BLE001
        return ParseStatus.INVALID
    if len(raw) > MAX_SLICE_INFO_BYTES:
        return ParseStatus.TOO_LARGE
    return raw


def _plate_index(node: ET.Element, fallback: int) -> int:
    for meta in node.findall("metadata"):
        if meta.get("key") == "index":
            try:
                return int(meta.get("value", ""))
            except ValueError:
                break
    return fallback


def _plate_weight(node: ET.Element) -> float | None:
    for meta in node.findall("metadata"):
        if meta.get("key") == "weight":
            return _float(meta.get("value"))
    return None


def parse_3mf(data: bytes) -> ParsedFile:
    """Parse a 3mf byte blob. Never raises on bad input."""
    raw = _read_slice_info(data)
    if isinstance(raw, ParseStatus):
        return ParsedFile(raw)
    # Reject any DTD: no entity expansion, no external references. NUL bytes
    # mean UTF-16/32, where the byte search below would miss a DTD.
    upper = raw.upper()
    if b"\x00" in raw or b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return ParsedFile(ParseStatus.INVALID)
    try:
        root = ET.fromstring(raw)  # noqa: S314 - DTD rejected above, size bounded
    except ET.ParseError:
        return ParsedFile(ParseStatus.INVALID)

    plates: list[Plate] = []
    for ordinal, node in enumerate(root.iter("plate"), start=1):
        if ordinal > MAX_PLATES:
            break
        plate = Plate(index=_plate_index(node, ordinal), weight=_plate_weight(node))
        for item in list(node.iter("filament"))[:MAX_FILAMENTS_PER_PLATE]:
            grams = _float(item.get("used_g"))
            color = normalize_hex(item.get("color"))
            if grams is None or grams <= 0 or color is None:
                continue
            try:
                slot = int(item.get("id", "0"))
            except ValueError:
                slot = 0
            plate.filaments.append(
                FilamentUse(
                    slot=slot,
                    material=normalize_material(item.get("type")),
                    color=color,
                    grams=grams,
                    filament_id=(item.get("tray_info_idx") or "").strip()[:32],
                )
            )
        if plate.filaments:
            plates.append(plate)

    if not plates:
        return ParsedFile(ParseStatus.NOT_SLICED)
    return ParsedFile(ParseStatus.OK, plates)
