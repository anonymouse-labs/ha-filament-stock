"""Static checks on the integration metadata."""

import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "filament_stock"


def _manifest() -> dict:
    return json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_has_required_keys() -> None:
    manifest = _manifest()
    for key in (
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
        "iot_class",
    ):
        assert manifest.get(key), key
    assert manifest["domain"] == "filament_stock"


def test_manifest_requirements_are_pinned() -> None:
    for requirement in _manifest()["requirements"]:
        assert "==" in requirement, requirement


def test_versions_match() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == _manifest()["version"]


def test_hacs_json() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == _manifest()["name"]
