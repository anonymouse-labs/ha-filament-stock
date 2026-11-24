"""Tests for the 3mf parser."""

import io
import zipfile

import pytest
from threemf import REFERENCE_PLATE, SLICE_INFO, build_3mf, slice_info

from custom_components.filament_stock import parser
from custom_components.filament_stock.parser import ParsedFile, ParseStatus, parse_3mf


def test_reference_plate() -> None:
    result = parse_3mf(build_3mf(slice_info([REFERENCE_PLATE])))
    assert result.status is ParseStatus.OK
    assert result.sliced
    assert len(result.plates) == 1
    assert result.plates[0].weight == pytest.approx(26.14)
    assert result.usage() == {
        ("PLA", "#101410"): pytest.approx(15.02),
        ("PLA", "#898989"): pytest.approx(11.12),
    }
    assert sum(result.usage().values()) == pytest.approx(26.14)


def test_multiple_plates_and_plate_usage() -> None:
    data = build_3mf(slice_info([REFERENCE_PLATE, [("PETG", "#ff0000", 5.0)]]))
    result = parse_3mf(data)
    assert [p.index for p in result.plates] == [1, 2]
    assert result.usage(2) == {("PETG", "#FF0000"): 5.0}
    assert result.usage(1) == {
        ("PLA", "#101410"): pytest.approx(15.02),
        ("PLA", "#898989"): pytest.approx(11.12),
    }
    assert result.usage(9) == {}
    assert len(result.usage()) == 3


def test_plate_index_falls_back_to_order() -> None:
    result = parse_3mf(build_3mf(slice_info([REFERENCE_PLATE] * 2, index=False)))
    assert [p.index for p in result.plates] == [1, 2]


def test_same_color_on_two_slots_is_summed() -> None:
    plate = [("PLA", "#101410", 2.0), ("pla", "#101410FF", 3.0)]
    result = parse_3mf(build_3mf(slice_info([plate])))
    assert result.usage() == {("PLA", "#101410"): 5.0}


def test_project_file_without_slice_info_is_not_sliced() -> None:
    assert parse_3mf(build_3mf(None)).status is ParseStatus.NOT_SLICED


def test_slice_info_without_filaments_is_not_sliced() -> None:
    config = '<?xml version="1.0"?><config><header/></config>'
    assert parse_3mf(build_3mf(config)).status is ParseStatus.NOT_SLICED


def test_zero_and_bad_values_are_skipped() -> None:
    config = (
        "<config><plate>"
        '<filament id="1" type="PLA" color="#000000" used_g="0"/>'
        '<filament id="x" type="PLA" color="#111111" used_g="1.5"/>'
        '<filament id="3" type="PLA" color="nope" used_g="2"/>'
        '<filament id="4" type="PLA" color="#222222" used_g="nan"/>'
        "</plate></config>"
    )
    result = parse_3mf(build_3mf(config))
    assert result.usage() == {("PLA", "#111111"): 1.5}
    assert result.plates[0].filaments[0].slot == 0


def test_not_a_zip_is_invalid() -> None:
    assert parse_3mf(b"not a zip").status is ParseStatus.INVALID


def test_broken_xml_is_invalid() -> None:
    assert parse_3mf(build_3mf("<config><plate>")).status is ParseStatus.INVALID


def test_doctype_is_rejected() -> None:
    config = (
        '<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "aaaa">]>'
        '<config><plate><filament type="PLA" color="#000000" used_g="1"/>'
        "</plate></config>"
    )
    assert parse_3mf(build_3mf(config)).status is ParseStatus.INVALID


def test_external_entity_is_rejected() -> None:
    config = (
        '<?xml version="1.0"?><!doctype c SYSTEM "file:///etc/passwd">'
        "<config>&xxe;</config>"
    )
    assert parse_3mf(build_3mf(config)).status is ParseStatus.INVALID


def test_utf16_doctype_is_rejected() -> None:
    config = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE c [<!ENTITY a "a">]>'
        '<config><plate><filament type="PLA" color="#000000" used_g="1"/>'
        "</plate></config>"
    ).encode("utf-16")
    assert parse_3mf(build_3mf(config)).status is ParseStatus.INVALID


def test_nested_filament_elements_are_found() -> None:
    config = (
        "<config><plate><group>"
        '<filament id="1" type="PLA" color="#101410" used_g="2.5"/>'
        "</group></plate></config>"
    )
    assert parse_3mf(build_3mf(config)).usage() == {("PLA", "#101410"): 2.5}


def test_corrupt_bytes_never_raise() -> None:
    data = build_3mf(slice_info([REFERENCE_PLATE]))
    for pos in range(len(data)):
        broken = bytearray(data)
        broken[pos] ^= 0xFF
        assert isinstance(parse_3mf(bytes(broken)), ParsedFile)


def test_encrypted_entry_is_invalid() -> None:
    data = bytearray(build_3mf(slice_info([REFERENCE_PLATE])))
    name = SLICE_INFO.encode()
    # Set the encryption bit in the general purpose flags (+8) of both headers.
    for signature, name_offset in ((b"PK\x03\x04", 30), (b"PK\x01\x02", 46)):
        start = 0
        while (header := data.find(signature, start)) != -1:
            if data[header + name_offset : header + name_offset + len(name)] == name:
                data[header + 6 if signature == b"PK\x03\x04" else header + 8] |= 1
            start = header + 4
    assert parse_3mf(bytes(data)).status is ParseStatus.INVALID


def test_too_large_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser, "MAX_ARCHIVE_BYTES", 10)
    data = build_3mf(slice_info([REFERENCE_PLATE]))
    assert parse_3mf(data).status is ParseStatus.TOO_LARGE


def test_zip_bomb_slice_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser, "MAX_SLICE_INFO_BYTES", 1024)
    bomb = slice_info([REFERENCE_PLATE]) + " " * 100_000
    assert parse_3mf(build_3mf(bomb)).status is ParseStatus.TOO_LARGE


def test_lying_zip_header_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header claims a small file, content is larger: bounded read stops it."""
    monkeypatch.setattr(parser, "MAX_SLICE_INFO_BYTES", 1024)
    data = bytearray(build_3mf(slice_info([REFERENCE_PLATE]) + " " * 5000))
    name = SLICE_INFO.encode()
    # Central directory header: name starts at +46, uncompressed size at +24.
    header = data.rfind(b"PK\x01\x02", 0, data.rfind(name))
    assert data[header + 46 : header + 46 + len(name)] == name
    data[header + 24 : header + 28] = (100).to_bytes(4, "little")
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        assert archive.getinfo(SLICE_INFO).file_size == 100
    result = parse_3mf(bytes(data))
    assert result.status in (ParseStatus.TOO_LARGE, ParseStatus.INVALID)


def test_too_many_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser, "MAX_ZIP_ENTRIES", 3)
    extra = {f"f{i}": b"x" for i in range(5)}
    data = build_3mf(slice_info([REFERENCE_PLATE]), extra)
    assert parse_3mf(data).status is ParseStatus.TOO_LARGE


def test_round_trip_dict() -> None:
    result = parse_3mf(build_3mf(slice_info([REFERENCE_PLATE, REFERENCE_PLATE])))
    restored = ParsedFile.from_dict(result.as_dict())
    assert restored == result
    assert ParsedFile.from_dict({"status": "not_sliced"}).status is (
        ParseStatus.NOT_SLICED
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "12 Gear BOX spare links v2.gcode.3mf",
            "12gearboxsparelinksv2",
        ),
        ("12_Gear_BOX_spare-links v2.3mf", "12gearboxsparelinksv2"),
        ("/data/Metadata/Plate_1.gcode", "plate1"),
        (r"C:\prints\Box.gcode.3mf", "box"),
        ("  ", ""),
    ],
)
def test_normalize_name(name: str, expected: str) -> None:
    assert parser.normalize_name(name) == expected
