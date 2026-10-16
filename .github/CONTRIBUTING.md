# Contributing to django-rls-tenants

Thank you for considering a contribution. This guide covers everything you need to get
started.

## Code of Conduct

This project and everyone participating in it is governed by the
[Contributor Covenant Code of Conduct](../CODE_OF_CONDUCT.md). By participating, you are
expected to uphold this code. Please report unacceptable behavior to dvoraj75@gmail.com.

## Prerequisites

- Python 3.12 or newer
- PostgreSQL 15–18
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Development Setup

```bash
# Clone the repository
git clone https://github.com/dvoraj75/django-rls-tenants.git
cd django-rls-tenants

# Install dependencies (dev + test + docs groups)
uv sync --group dev --group test --group docs

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

Pre-commit hooks run these automatically on every commit. The hooks invoke Ruff and mypy
through `uv run`, so they use the exact versions pinned in `uv.lock` -- the same versions
CI runs.

## Documentation

Build the documentation locally before opening a pull request (requires the `docs`
dependency group):

```bash
uv run mkdocs build --strict
```

CI runs this exact command on every pull request. `--strict` turns broken links and
missing `mkdocstrings` symbols into build failures.

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

# On a non-superuser test database role (the marked test fails instead of skipping)
uv run pytest -m "not superuser_only"
```

CI fails any PostgreSQL leg that skips a test (`RLS_FAIL_ON_SKIPPED=1`, hook in
`tests/conftest.py`), so never add a runtime `pytest.skip()`; use a registered marker such
as `superuser_only` and deselect it. Django deprecation warnings are test errors. Tests that
need Django's connection pool carry `psycopg3_only`; they are deselected (not skipped) on
psycopg2, and only the psycopg 3 CI leg runs them. The GUC affinity guard applies the active
context before every statement that runs through Django's cursor: a test that checks what a
session itself holds reads it with a driver cursor (`tests/session_gucs.py`).

CI runs Python 3.12-3.14 x Django 5.2-6.1 on PostgreSQL 16 with psycopg2, plus a psycopg 3
leg, and PostgreSQL 15 and 18 legs with the oldest and newest supported Django. Those legs
connect as a superuser, which bypasses RLS, so one more leg runs the suite as a plain role
with `-m "not superuser_only"`: a test of RLS behaviour must pass there too (under the
superuser it requests `enforce_rls`, a fixture from the library's own pytest plugin). CI also
installs the built wheel into a clean virtualenv and runs the example app against it, and
the release workflow refuses a tag whose version has no `## [X.Y.Z]` heading in
`CHANGELOG.md`. See the full
[contributing guide](https://dvoraj75.github.io/django-rls-tenants/development/contributing/).

The supported-versions policy lives in the
[installation guide](https://dvoraj75.github.io/django-rls-tenants/getting-started/installation/#supported-versions);
`tests/test_support_matrix.py` fails when any file disagrees with it. A weekly canary
workflow runs the suite against Django `main`, the next Python and the next PostgreSQL;
it blocks nothing, and a red run means a matrix review is due.

### Test Organization

```
tests/
├── conftest.py              # Shared fixtures
├── test_pytest_plugin.py    # The shipped pytest plugin
├── test_rls/                # rls/ layer unit tests
├── test_tenants/            # tenants/ layer unit tests
├── test_integration/        # End-to-end tests
└── test_layering.py         # Import boundary enforcement
```

## Pull Request Process

1. Fork the repository and create a branch from `develop`.
2. Add or update tests for your changes.
3. Ensure the full test suite passes: `uv run pytest`.
4. Ensure linting and type checks pass: `uv run ruff check . && uv run mypy django_rls_tenants`.
5. Ensure the docs build cleanly: `uv run mkdocs build --strict`.
6. Update the `[Unreleased]` section in `CHANGELOG.md`.
7. Open a pull request against `develop` with a clear description of the change.

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
