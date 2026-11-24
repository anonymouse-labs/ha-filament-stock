# Contributing

Issues and pull requests are welcome.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/pytest
```

- Keep pull requests small and focused, with tests for changed behavior.
- User-facing texts go in `translations/en.json` and `translations/nl.json`.
- Never commit real slicer files, tokens, service account keys or other secrets. Build test files in code, see `tests/threemf.py`.
