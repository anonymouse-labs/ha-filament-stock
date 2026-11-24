"""Static checks on the translation files."""

from collections.abc import Iterator
import json
from pathlib import Path
import re

import pytest

from custom_components.filament_stock.config_flow import DRIVE_LINKS

TRANSLATIONS = (
    Path(__file__).parents[1] / "custom_components/filament_stock/translations"
)


def _load(lang: str) -> dict:
    return json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _keys(node: object, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    return set().union(*(_keys(v, f"{prefix}.{k}") for k, v in node.items()))


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_braces_are_placeholders(lang: str) -> None:
    """A stray brace breaks HA's message formatting."""
    for text in _strings(_load(lang)):
        for match in re.findall(r"\{[^}]*\}|[{}]", text):
            assert re.fullmatch(r"\{[a-z_]+\}", match), (lang, match, text)


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_drive_placeholders_are_provided(lang: str) -> None:
    drive = _load(lang)["config"]["step"]["drive"]
    used = set(re.findall(r"\{([a-z_]+)\}", json.dumps(drive)))
    assert used <= {*DRIVE_LINKS, "email"}


def test_languages_have_the_same_keys() -> None:
    assert _keys(_load("en")) == _keys(_load("nl"))


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_no_html(lang: str) -> None:
    """hassfest rejects anything that looks like an HTML tag."""
    for text in _strings(_load(lang)):
        assert not re.search(r"<[^>]+>", text), text
