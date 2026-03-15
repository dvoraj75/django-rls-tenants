# Contributing

Thank you for considering a contribution to django-rls-tenants. This guide covers
everything you need to get started.

## Prerequisites

- Python 3.11+
- PostgreSQL 15+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Development Setup

```bash
# Clone the repository
git clone https://github.com/dvoraj75/django-rls-tenants.git
cd django-rls-tenants

# Install all dependencies (dev + test + docs)
uv sync --group dev --group test --group docs

# Install pre-commit hooks
uv run pre-commit install

# Create a test database
createdb django_rls_tenants_test

# Run the test suite
uv run pytest
```

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting,
and [mypy](https://mypy-lang.org/) for type checking. All configuration lives in
`pyproject.toml`.

```bash
# Lint
uv run ruff check .

# Lint with auto-fix
uv run ruff check --fix .

# Format
uv run ruff format .

# Type check (production code only)
uv run mypy django_rls_tenants
```

Pre-commit hooks run Ruff and mypy automatically on every commit.

### Key Style Rules

- **Line length**: 99 characters.
- **Every `.py` file** starts with a module docstring followed by `from __future__ import annotations`.
- **Absolute imports only** -- no relative imports.
- **Google-style docstrings** with `Args:`, `Returns:`, `Raises:` sections.
- **Modern type syntax**: `str | None` (not `Optional[str]`), `list[str]` (not `List[str]`).
- Always use specific `# noqa: XXXX` codes with an explanatory comment.

## Testing

Tests require a running PostgreSQL instance. Configure the connection via environment
variables or edit `tests/settings.py`.

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov

# Run a specific test file
uv run pytest tests/test_rls/test_guc.py

# Run a single test
uv run pytest tests/test_rls/test_guc.py::test_set_get_roundtrip

# Run only integration tests
uv run pytest -m integration

# Keyword filter
uv run pytest -k "guc and not local"
```

### Test Organization

```
tests/
├── conftest.py              # Shared fixtures
├── settings.py              # Django settings for test suite
├── test_app/                # Test-only Django app (models, migrations)
├── test_rls/                # rls/ layer unit tests
├── test_tenants/            # tenants/ layer unit tests
├── test_integration/        # End-to-end tests (@pytest.mark.integration)
└── test_layering.py         # Import boundary enforcement
```

### Test Conventions

- **pytest-style**: plain `assert` statements, `pytest.raises()` for exceptions.
- **File naming** mirrors source: `django_rls_tenants/rls/guc.py` maps to `tests/test_rls/test_guc.py`.
- **Function naming**: `test_{what}` or `test_{action}_{expected_behavior}`.
- **Module docstring**: `"""Tests for django_rls_tenants.{module.path}."""`
- **Fixtures**: descriptive nouns (`tenant_a`, `admin_user`).
- **DB access**: `pytestmark = pytest.mark.django_db` at module level.

## Architecture

The library has two internal layers with a strict import boundary:

- **`rls/`** -- Generic PostgreSQL RLS primitives. **Zero imports from `tenants/`.**
- **`tenants/`** -- Django multitenancy built on `rls/`.

This boundary is enforced by `tests/test_layering.py`. Do not introduce imports
from `tenants/` into `rls/`.

## Pull Request Process

1. Fork the repository and create a branch from `main`.
2. Add or update tests for your changes.
3. Ensure the full test suite passes: `uv run pytest`.
4. Ensure linting and type checks pass: `uv run ruff check . && uv run mypy django_rls_tenants`.
5. Update the `[Unreleased]` section in `CHANGELOG.md`.
6. Open a pull request with a clear description of the change.

## Commit Messages

Write concise commit messages that explain **why**, not just **what**. Use imperative
mood (e.g., "Add bypass flag for pre-auth requests" not "Added bypass flag...").

## Reporting Bugs

Use the [bug report template](https://github.com/dvoraj75/django-rls-tenants/issues/new?template=bug_report.yml)
on GitHub Issues.

## Requesting Features

Use the [feature request template](https://github.com/dvoraj75/django-rls-tenants/issues/new?template=feature_request.yml)
on GitHub Issues.
