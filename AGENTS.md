# AGENTS.md

> Guidelines for AI coding agents operating in this repository.

## Project Overview

Django library providing database-enforced multitenancy using PostgreSQL Row-Level Security (RLS).
Python 3.11+, Django 4.2+, PostgreSQL 15+. Uses `uv` for dependency management and `hatchling` as build backend.

## Build & Dependencies

```bash
uv sync --group dev --group test   # install all deps (dev + test)
uv run pre-commit install          # install pre-commit hooks
```

## Lint / Format / Type-Check

```bash
uv run ruff check .                # lint (all rules in pyproject.toml)
uv run ruff check --fix .          # lint with auto-fix
uv run ruff format .               # format (Black-compatible)
uv run ruff format --check .       # format check only (CI mode)
uv run mypy django_rls_tenants     # strict type-check (production code only)
```

Pre-commit hooks run ruff check, ruff format, and mypy automatically on every commit.

## Testing

Tests require a running PostgreSQL instance. Configure via env vars
(`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`)
or use defaults from `tests/settings.py` (db: `django_rls_tenants_test`, user/pass: `postgres`).

```bash
uv run pytest                              # run all tests
uv run pytest tests/test_rls/test_guc.py   # run a single test file
uv run pytest tests/test_rls/test_guc.py::test_set_get_roundtrip  # single test
uv run pytest -k "guc and not local"       # keyword filter
uv run pytest -m integration               # only integration tests (need live PG)
uv run pytest --cov                        # with coverage (fail_under = 90%)
```

### Test Organization

```
tests/
├── conftest.py              # shared fixtures
├── settings.py              # Django settings for test suite
├── test_rls/                # unit tests for rls/ layer
├── test_tenants/            # unit tests for tenants/ layer
├── test_integration/        # end-to-end tests (marked @pytest.mark.integration)
└── test_layering.py         # enforces rls/ ← tenants/ import boundary
```

## Architecture

The library has two internal layers with a strict import boundary:

- **`django_rls_tenants/rls/`** — Generic PostgreSQL RLS primitives (GUC helpers,
  `RLSConstraint`, context managers). **Zero imports from `tenants/`.**
- **`django_rls_tenants/tenants/`** — Django multitenancy built on `rls/` (models,
  managers, middleware, config, testing utilities).

This boundary is enforced by `tests/test_layering.py`. Never import from `tenants/` in `rls/`.

## Code Style

### General Formatting

- **Line length:** 99 characters.
- **Formatter:** ruff format (Black-compatible). Double quotes for strings.
- **Trailing commas** in all multi-line constructs (enforced by formatter).
- **Two blank lines** between top-level definitions; one blank line between methods.

### Imports

- **Every `.py` file** with content must start with a module docstring followed by
  `from __future__ import annotations`.
- **Import order** (enforced by ruff isort): stdlib → third-party → first-party.
  Groups separated by a blank line. First-party package: `django_rls_tenants`.
- **Absolute imports only.** No relative imports.
- **`from X import Y`** preferred over bare `import X` for specific symbols.
- Combine multiple imports from the same module on one line.
- Use `if TYPE_CHECKING:` blocks for imports needed only by type annotations.
  Ruff rule `TCH` enforces this.

```python
"""Module docstring."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import models

from django_rls_tenants.rls.guc import set_guc

if TYPE_CHECKING:
    from django_rls_tenants.tenants.types import TenantUser
```

### Type Annotations

- **mypy strict mode** is enabled for production code (`django_rls_tenants/`).
  Tests have `disallow_untyped_defs = false`.
- All production functions must have full type annotations including return types.
  Use `-> None` for functions returning nothing.
- Modern union syntax: `str | None` (not `Optional[str]`).
- Modern generics: `list[str]`, `dict[str, int]` (not `List`, `Dict`).
- Use `collections.abc` for abstract types: `Iterator`, `Callable`, `Sequence`.
- Use `typing.Protocol` with `@runtime_checkable` for structural subtyping.

### Naming Conventions

- **Classes:** `PascalCase` — `RLSConstraint`, `RLSProtectedModel`, `TenantQuerySet`.
- **Functions/methods:** `snake_case` — `set_guc`, `tenant_context`, `clear_bypass_flag`.
- **Constants/settings keys:** `UPPER_SNAKE_CASE` — `TENANT_MODEL`, `GUC_PREFIX`.
- **Private members:** leading underscore — `_get_arg_from_signature`, `self._rls_user`.
- **Module-level singletons:** `snake_case` — `rls_tenants_config = RLSTenantsConfig()`.

### Docstrings

Google-style docstrings with `Args:`, `Returns:`, `Raises:` sections.
Use reStructuredText inline markup (double backticks) for code references.

```python
def set_guc(name: str, value: str, *, is_local: bool = False) -> None:
    """Set a PostgreSQL session variable (GUC).

    Args:
        name: Variable name (e.g., ``"rls.current_tenant"``).
        value: Variable value as string.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).

    Raises:
        RuntimeError: If ``is_local=True`` outside ``transaction.atomic()``.
    """
```

- **Module docstrings** are mandatory on every file.
- One-line docstrings for simple functions: `"""Clear a GUC variable."""`

### Error Handling

- **Custom exceptions** live in `django_rls_tenants/exceptions.py` (package root,
  usable by both `rls/` and `tenants/` layers):
  - `RLSTenantError(Exception)` — base for all library errors.
  - `NoTenantContextError(RLSTenantError)` — missing tenant context (e.g.,
    `tenant_context(None)`, non-admin with `rls_tenant_id=None`).
  - `RLSConfigurationError(RLSTenantError)` — invalid/missing config (e.g.,
    missing `TENANT_MODEL`).
- The **`rls/` layer** still uses stdlib exceptions (`ValueError` for input
  validation / SQL injection guards, `RuntimeError` for misuse like SET LOCAL
  outside a transaction). This keeps the generic layer free of tenant-specific
  concerns.
- Error messages must be **descriptive f-strings** explaining both the problem and
  how to fix it (`TRY003` is intentionally ignored).
- **Logging:** `logger = logging.getLogger("django_rls_tenants")`. Use `%s` format
  strings in log calls (not f-strings) per Ruff rule `G`.
- Cleanup: prefer `try/finally` for resource cleanup (GUC state restoration).

### Inline Suppressions

Always use specific rule codes with an explanatory comment:

```python
SECRET_KEY = "..."  # noqa: S105  -- test-only secret
sql = f"... {col} ..."  # noqa: S608  -- developer-controlled, not user input
```

Never use blanket `# noqa` without a code.

### `__all__` Exports

Sub-package `__init__.py` files define `__all__` listing the public API.
Keep entries alphabetically sorted. The top-level `__init__.py` re-exports
the most common symbols with grouped comments.

### Test Conventions

- **pytest-style** — plain `assert` statements, `pytest.raises()` for exceptions.
- **File naming** mirrors source: `django_rls_tenants/rls/guc.py` → `tests/test_rls/test_guc.py`.
- **Function naming:** `test_{what}` or `test_{action}_{expected_behavior}`.
- **Module docstring:** `"""Tests for django_rls_tenants.{module.path}."""`
- **Fixtures:** descriptive nouns (`tenant_a`, `admin_user`). Use `@pytest.fixture`.
- **DB access:** `pytestmark = pytest.mark.django_db` at module level, or
  `@pytest.mark.django_db(transaction=True)` for transaction isolation tests.
- Ruff relaxations for tests: `S101` (assert), `ARG` (unused fixtures),
  `SLF001` (private access), `PLR2004` (magic values) are all permitted.

## Example Project

The `example/` directory contains a self-contained multi-tenant Django demo app (notes app).
It showcases `RLSProtectedModel`, `RLSTenantMiddleware`, `TenantUser` protocol, and `admin_context`.

```bash
cd example/
docker compose up              # start db + web (runs migrate + seed + runserver)
docker compose up --build      # rebuild after library or example changes
docker compose down -v         # stop and destroy database volume
```

The example has its own `Dockerfile` that installs the library from source (parent directory).
Build context is the repo root so library code changes are picked up on rebuild.

**Code style for `example/`:** The example is a demo app, not library code. It does **not**
use `from __future__ import annotations`, strict mypy, or the full ruff rule set. It follows
standard Django conventions (relative imports within apps, function-based views, no type annotations).

### Example structure

```
example/
├── docker-compose.yml   Docker Compose (db + web)
├── Dockerfile           Installs library from source + example app
├── manage.py            Django management script
├── demo/                Project settings, urls, wsgi/asgi
├── tenants/             Tenant model (shared, not RLS-protected)
├── accounts/            Custom User with TenantUser protocol
├── notes/               RLS-protected Note model, views, templates, seed command
└── docker/              PostgreSQL init (creates non-superuser role for RLS)
```

## CI

GitHub Actions runs lint + type-check and a test matrix across
Python {3.11, 3.12, 3.13, 3.14} × Django {4.2, 5.0, 5.1, 5.2, 6.0} on every push/PR.
