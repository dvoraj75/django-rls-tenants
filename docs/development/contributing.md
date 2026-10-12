# Contributing

Thank you for considering a contribution to django-rls-tenants. This guide covers
everything you need to get started.

## Code of Conduct

This project and everyone participating in it is governed by the
[Contributor Covenant Code of Conduct](code-of-conduct.md). By participating, you are
expected to uphold this code. Please report unacceptable behavior to dvoraj75@gmail.com.

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

Pre-commit hooks run Ruff and mypy automatically on every commit. The hooks invoke both
tools through `uv run`, so they use the exact versions pinned in `uv.lock` -- the same
versions CI runs.

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

### Skipped tests and the `superuser_only` marker

CI treats a skipped test as a failure. Every PostgreSQL leg sets `RLS_FAIL_ON_SKIPPED=1`,
and a `pytest_sessionfinish` hook in `tests/conftest.py` (mirrored in
`example/tests/conftest.py`) fails the session when anything was skipped. A skipped RLS
test is indistinguishable from a leg whose database role never materialised, so the suite
contains no runtime `pytest.skip()` calls. Put the intent on the command line instead:
tests that need a PostgreSQL superuser (for example to create a `BYPASSRLS` role) carry
`@pytest.mark.superuser_only` and **fail** with an explanatory message on a plain role.
Deselect them when your test database role is not a superuser:

```bash
uv run pytest -m "not superuser_only"
```

Locally `RLS_FAIL_ON_SKIPPED` is unset and skips behave as usual. New markers must be
registered in `pyproject.toml` (`--strict-markers` is on).

## Documentation

The documentation site is built with [MkDocs](https://www.mkdocs.org/) and
[mkdocstrings](https://mkdocstrings.github.io/). Build it locally before opening a pull
request (requires the `docs` dependency group):

```bash
uv run mkdocs build --strict
```

CI runs this exact command on every pull request. `--strict` turns warnings such as broken
links and missing `mkdocstrings` symbols (for example, after renaming or removing a public
API) into build failures.

## Architecture

The library has two internal layers with a strict import boundary:

- **`rls/`** -- Generic PostgreSQL RLS primitives. **Zero imports from `tenants/`.**
- **`tenants/`** -- Django multitenancy built on `rls/`.

This boundary is enforced by `tests/test_layering.py`. Do not introduce imports
from `tenants/` into `rls/`.

## Continuous Integration

Every push and pull request runs:

- **Lint & Type Check** -- `uv lock --check`, Ruff (check + format) and mypy.
- **Docs** -- `mkdocs build --strict`.
- **Test matrix** -- Python 3.11-3.14 x Django 4.2-6.0 on PostgreSQL 16.
- **PostgreSQL 15 and 17 legs** -- the oldest and newest supported Django (4.2 on
  Python 3.12, 6.0 on Python 3.14) on both, so the "PostgreSQL >= 15" requirement is
  verified at each end rather than assumed.
- **Example app against the built wheel** -- `uv build`, an assertion on the wheel's
  file list (only `django_rls_tenants/` and `*.dist-info/`, every package directory has
  an `__init__.py`, `py.typed`, the management commands and `contrib.celery` are present,
  no `tests/` or `example/`), then an install into a clean virtualenv -- never editable,
  never with the source tree on `sys.path` -- followed by `manage.py check`,
  `makemigrations --check --dry-run` and the example test suite as a non-superuser role.
  A packaging defect fails here instead of at a user's `pip install`.

Both test matrices explicitly select their Python interpreter, verify the active Python
and Django versions, and run pytest with `uv run --no-sync` so the Django override is
not replaced by the lockfile version.

All PostgreSQL legs run with `RLS_FAIL_ON_SKIPPED=1` (see
[Skipped tests](#skipped-tests-and-the-superuser_only-marker)).

Publishing a GitHub release runs a **preflight** job before anything else: the tag must
be `vX.Y.Z`, `X.Y.Z` must equal `version` in `pyproject.toml`, and `CHANGELOG.md` must
contain a `## [X.Y.Z]` heading. Make the release commit (version bump + changelog
heading) first, then create the release.

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
