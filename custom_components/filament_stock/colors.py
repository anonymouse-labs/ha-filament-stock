"""Color helpers: hex normalization and perceptual distance (CIEDE2000).

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

import math
import re

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")

type Lab = tuple[float, float, float]


def normalize_hex(value: str | None) -> str | None:
    """Return '#RRGGBB' in upper case, or None when the value is not a color.

    An 8-digit value is read as RRGGBBAA and the alpha byte is dropped.
    """
    # shortcut: assumes RRGGBBAA for 8 digits, confirm against a non-white cloud spool
    if not value:
        return None
    match = _HEX_RE.match(value.strip())
    if not match:
        return None
    return f"#{match.group(1).upper()}"


def hex_to_rgb(value: str | None) -> tuple[int, int, int] | None:
    """Return (red, green, blue) as 0-255 integers, or None when not a color."""
    normalized = normalize_hex(value)
    if normalized is None:
        return None
    return (
        int(normalized[1:3], 16),
        int(normalized[3:5], 16),
        int(normalized[5:7], 16),
    )


def _srgb_to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def hex_to_lab(value: str) -> Lab:
    """Convert '#RRGGBB' to CIE L*a*b* (D65 white point)."""
    rgb = hex_to_rgb(value)
    if rgb is None:
        raise ValueError(f"not a hex color: {value!r}")
    r, g, b = (_srgb_to_linear(channel / 255) for channel in rgb)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def ciede2000(lab1: Lab, lab2: Lab) -> float:
    """CIEDE2000 color difference (kL = kC = kH = 1)."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_mean7 = ((c1 + c2) / 2) ** 7
    g = 0.5 * (1 - math.sqrt(c_mean7 / (c_mean7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0

    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dhp_big = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))

    lp_mean = (l1 + l2) / 2
    cp_mean = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_mean = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_mean = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hp_mean = (h1p + h2p + 360) / 2
    else:
        hp_mean = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(hp_mean - 30))
        + 0.24 * math.cos(math.radians(2 * hp_mean))
        + 0.32 * math.cos(math.radians(3 * hp_mean + 6))
        - 0.20 * math.cos(math.radians(4 * hp_mean - 63))
    )
    d_theta = 30 * math.exp(-(((hp_mean - 275) / 25) ** 2))
    cp_mean7 = cp_mean**7
    r_c = 2 * math.sqrt(cp_mean7 / (cp_mean7 + 25**7))
    s_l = 1 + (0.015 * (lp_mean - 50) ** 2) / math.sqrt(20 + (lp_mean - 50) ** 2)
    s_c = 1 + 0.045 * cp_mean
    s_h = 1 + 0.015 * cp_mean * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c

    return math.sqrt(
        (dlp / s_l) ** 2
        + (dcp / s_c) ** 2
        + (dhp_big / s_h) ** 2
        + r_t * (dcp / s_c) * (dhp_big / s_h)
    )


def color_distance(hex1: str, hex2: str) -> float:
    """Perceptual distance between two hex colors (CIEDE2000)."""
    return ciede2000(hex_to_lab(hex1), hex_to_lab(hex2))
