"""Tests for color helpers."""

import pytest

from custom_components.filament_stock.colors import (
    ciede2000,
    color_distance,
    hex_to_lab,
    normalize_hex,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#101410", "#101410"),
        ("101410", "#101410"),
        ("#ffffff", "#FFFFFF"),
        (" #00ff00 ", "#00FF00"),
        ("#FFFFFFFF", "#FFFFFF"),
        ("#11223344", "#112233"),
        ("", None),
        (None, None),
        ("#12345", None),
        ("#GGGGGG", None),
        ("#1234567", None),
    ],
)
def test_normalize_hex(value: str | None, expected: str | None) -> None:
    assert normalize_hex(value) == expected


# Reference pairs from Sharma, Wu and Dalal (2005), "The CIEDE2000
# color-difference formula: implementation notes".
@pytest.mark.parametrize(
    ("lab1", "lab2", "expected"),
    [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((50.0, 2.5, 0.0), (61.0, -5.0, 29.0), 22.8977),
    ],
)
def test_ciede2000_reference(lab1, lab2, expected) -> None:
    assert ciede2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)
    assert ciede2000(lab2, lab1) == pytest.approx(expected, abs=1e-4)


def test_hex_to_lab_white_and_black() -> None:
    white = hex_to_lab("#FFFFFF")
    assert white[0] == pytest.approx(100.0, abs=0.01)
    assert abs(white[1]) < 0.01
    assert abs(white[2]) < 0.01
    assert hex_to_lab("#000000")[0] == pytest.approx(0.0, abs=0.01)


def test_hex_to_lab_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a hex color"):
        hex_to_lab("red")


def test_color_distance() -> None:
    assert color_distance("#101410", "#101410") == 0
    # Near-black project color against a black spool: small distance.
    assert color_distance("#101410", "#000000") < 10
    # Black against grey: large distance.
    assert color_distance("#101410", "#898989") > 30
