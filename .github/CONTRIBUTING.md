# Contributing to django-rls-tenants

Thank you for considering a contribution. This guide covers everything you need to get
started.

## Prerequisites

- Python 3.11+
- PostgreSQL 15+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Development Setup

```bash
# Clone the repository
git clone https://github.com/dvoraj75/django-rls-tenants.git
cd django-rls-tenants

# Install dependencies (including dev + test groups)
uv sync --group dev --group test

# Install pre-commit hooks
uv run pre-commit install

# Create a test database
createdb django_rls_tenants_test

# Run the test suite
uv run pytest
```

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.
All configuration lives in `pyproject.toml`.

```bash
# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run mypy django_rls_tenants
```

Pre-commit hooks run these automatically on every commit.

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

# Run only integration tests
uv run pytest -m integration
```

### Test Organization

```
tests/
├── conftest.py              # Shared fixtures
├── test_rls/                # rls/ layer unit tests
├── test_tenants/            # tenants/ layer unit tests
├── test_integration/        # End-to-end tests
└── test_layering.py         # Import boundary enforcement
```

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

## Architecture Notes

The library has two internal layers:

- **`rls/`** -- Generic PostgreSQL RLS primitives. Zero imports from `tenants/`.
- **`tenants/`** -- Django multitenancy built on `rls/`.

This boundary is enforced by a test (`tests/test_layering.py`). Do not introduce imports
from `tenants/` into `rls/`.
