# Contributing

Thank you for contributing to the DSpace Python client.

## Setup

Create and activate a virtual environment (see [README → Installing](README.md#installing) for OS-specific commands), then:

```bash
pip install --upgrade pip
pip install -e ".[dev]"
```

## Checks

Run these from the project root with the venv activated:

```bash
pytest tests/
ruff check dspace_client/ examples/ tests/
mypy dspace_client/
```

## Commit messages

Use clear, imperative subject lines focused on **why** the change matters (for example: `fix: repair adaptive concurrency ramp-up`).

## Pull requests

- Keep changes focused; unrelated refactors belong in separate commits.
- Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.
- Ensure tests pass before opening a PR.

See also the [Contributing section in README.md](README.md).
