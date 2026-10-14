# AGENTS.md

> Guidelines for AI coding agents operating in this repository.

## Project Overview

Django library providing database-enforced multitenancy using PostgreSQL Row-Level Security (RLS).
Python 3.12+, Django 5.2+, PostgreSQL 15–18. Uses `uv` for dependency management and `hatchling` as build backend.

## Build & Dependencies

```bash
uv sync --group dev --group test --group docs   # install all deps (dev + test + docs)
uv run pre-commit install          # install pre-commit hooks
```

## Lint / Format / Type-Check

```bash
uv run ruff check .                # lint (all rules in pyproject.toml)
uv run ruff check --fix .          # lint with auto-fix
uv run ruff format .               # format (Black-compatible)
uv run ruff format --check .       # format check only (CI mode)
uv run mypy django_rls_tenants     # strict type-check (production code only)
uv run mkdocs build --strict       # docs build (needs --group docs; CI runs this on PRs)
```

Pre-commit hooks run ruff check, ruff format, and mypy automatically on every commit,
all via `uv run`, so they match the versions pinned in `uv.lock`.

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
uv run pytest -m "not superuser_only"      # on a non-superuser DB role (see below)
uv run pytest --cov                        # with coverage (fail_under = 90%)
```

**Zero-skipped rule.** Every PostgreSQL CI leg sets `RLS_FAIL_ON_SKIPPED=1`; a
`pytest_sessionfinish` hook in `tests/conftest.py` (mirrored in `example/tests/conftest.py`)
fails the session if anything was skipped. Never add a runtime `pytest.skip()`. Tests that
need a superuser connection carry `@pytest.mark.superuser_only` (registered in
`pyproject.toml`) and `pytest.fail()` on a plain role; deselect with `-m "not superuser_only"`.

**psycopg 3 marker.** Tests that need Django's connection pool (psycopg 3 plus
`psycopg_pool`) carry `@pytest.mark.psycopg3_only`. `pytest_collection_modifyitems` in
`tests/conftest.py` deselects them when Django runs on psycopg2 (reported as "deselected",
not skipped, so the zero-skipped rule holds); only the psycopg 3 CI leg runs them, and a
missing `psycopg_pool` there fails them. They use the `pooled` fixture, a real pool of one
connection. To run them locally, install `psycopg[binary,pool]` into a separate
environment: installing psycopg 3 switches Django's driver for the whole process.

**Plain-role rule.** CI's `test-plain-role` job runs the whole library suite as a
non-superuser role (`-m "not superuser_only"`). A superuser bypasses RLS, so any test that
asserts database-level filtering (including `for_user()` results) must request the
`enforce_rls` fixture: it switches a superuser connection to a plain role and is a no-op
on one. Rows a test needs must be created inside `admin_context()` / `tenant_context()`,
since a plain role cannot insert them without a context.

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
  `RLSConstraint`, `RLSM2MConstraint`, context managers). **Zero imports from `tenants/`.**
- **`django_rls_tenants/tenants/`** — Django multitenancy built on `rls/` (models,
  managers, middleware, config, testing utilities).
- **`django_rls_tenants/operations.py`** — Migration operations (`AddM2MRLSPolicy`)
  for M2M join table RLS policies. Imports from `rls/` for shared SQL builders and validation.
- **`django_rls_tenants/pool.py`** — `reset_rls_gucs()`, the `reset` callback for Django's
  psycopg connection pool, and the single statement that clears every GUC the policies
  read (used by the `connection_created` handler too). `settings.py` imports it before
  Django is set up, so it must not import `tenants/` at module level.

This boundary is enforced by `tests/test_layering.py`. Never import from `tenants/` in `rls/`.

### M2M RLS Support

M2M through tables get `EXISTS`-based subquery RLS policies. Three components
cooperate:

- `RLSM2MConstraint` (in `rls/constraints.py`) — constraint for `Meta.constraints`
- `AddM2MRLSPolicy` (in `operations.py`) — reversible migration operation
- `register_m2m_rls()` (in `tenants/models.py`) — auto-detection in `AppConfig.ready()`

All three share SQL builder functions (`_build_m2m_conditions`,
`_build_m2m_create_sql`, `_build_m2m_drop_sql`) from `rls/constraints.py` to
avoid duplication. All SQL-interpolated inputs are validated via
`_validate_field_name`, `_validate_model_path`, `_validate_pk_type`, and
`_validate_guc_name_for_ddl`.

### Multi-Database Support

The middleware sets GUC variables on all database aliases listed in
`RLS_TENANTS["DATABASES"]` (default: `["default"]`). A `connection_created`
signal handler in `apps.py` also sets GUCs on lazily-created connections,
but only on the thread that set the request's GUCs: the "GUCs set" `ContextVar`
holds that thread's marker (`_mark_gucs_set()`, `_were_gucs_set_on_this_thread()`
in `middleware.py`). `ContextVar`s are copied into `thread_sensitive=False` hops,
whose connections the cleanup never reaches, so on any other thread the handler
clears the GUCs instead (#130), and the GUC affinity guard (below) then applies the
copied context to that thread's statements (#41). Code that sets the flag from a
different thread than the one that set the GUCs must pass that thread's marker (the
async path takes it from the enter hop); `_mark_gucs_set()` alone marks the current
thread. The safety-net `request_finished` handler clears GUCs on all configured aliases,
and ends the guard's context. The sync `__call__` also cleans up when a `BaseException`
skips `process_response`.

On an alias with `OPTIONS["pool"]` (Django's psycopg pool, `_is_pooled()` in `apps.py`),
Django sends `connection_created` on every checkout, and the handler first clears every
GUC the policies read, with or without a request context: `_clear_new_connection_gucs()`
runs `_clear_policy_gucs()` from `pool.py`, one statement on the connection the signal
sent (not `connections[alias]`), committed under `AUTOCOMMIT=False`, because a pool keeps
session GUCs between borrowers and the previous borrower's cleanup may not have reached
the connection (#96). If it fails on a pooled connection, the handler closes the
connection (the pool discards it) and re-raises: fail closed. The request thread's GUCs
are applied after it. The #130 clear for other threads uses the same statement.
`_policy_guc_names()` collects the names from the configured `GUC_PREFIX` GUCs and the
installed models' constraints (`RLSConstraint` GUCs and `extra_bypass_flags`,
`RLSM2MConstraint` GUCs), keeping only dotted custom-GUC names; hand-written policies and
`AddM2MRLSPolicy` with non-default names are not covered. The registry part is cached once
`apps.ready` and dropped on an `INSTALLED_APPS` change. The safety net skips a closed
connection (with a pool, Django has already returned it). System check `W010` asks for
`reset_rls_gucs` as the pool's `reset`.

When modifying middleware or GUC-related code, ensure all database iteration
uses `conf.DATABASES` rather than hardcoding `"default"`.

### GUC Affinity Guard

`tenants/affinity.py` (#41). The `connection_created` handler (`_prepare_new_connection()`
in `apps.py`) calls `install()` on every PostgreSQL connection: `_guard` goes first in
`execute_wrappers` (Django's `execute_wrapper()` pops the last one on exit), the
connection's `_commit` / `_rollback` are wrapped, and a `_SessionGUCs` tracker is attached.
Before each statement on an alias in `conf.DATABASES` with `USE_LOCAL_SET=False`, the guard
compares the active context's values (`state._get_context_gucs()`: a `ContextGucs` named
tuple, or `None`) with what the tracker says the session holds and, if they differ, runs
`write_gucs()` (one `set_config()` pair) first; a failure propagates. So every configured
alias follows the context, whichever alias it was entered on. The `ContextVar` holds a
`_Live` holder that `_reset_context_gucs()` / `_clear_context_gucs()` end for every copy,
so a hop or task inherits a context only while its block is live. A block's holder
(`block=True`) compares equal to anything, so asgiref's copy-back (`!=`) never hands a
cancelled `sync_to_async` call's still-running block to its caller; only
`process_request` sets `block=False`, since `MiddlewareMixin` runs it in a hop of its own.
The wrapped `_commit` / `_rollback` read the libpq status before and after (a commit of a
failed transaction is a rollback; a call that ended nothing is ignored).

Rules for code that sets a context or writes the admin/tenant GUCs:

- Every library entry point that sets the tenant `ContextVar`s sets the guard's too, to
  exactly the values it writes: the context managers and `_rls_scope()` use
  `context._rls_state()`, which sets and resets all three; the middleware keeps what
  `_set_context_gucs()` returned on the request (or in `__acall__`) and ends it first, with
  `_end_context_gucs()`. They also pass the enclosing holder (`state._current_live()`, read
  on entry) to `_restore_tenant_gucs(..., enclosing=...)`, which clears instead of
  restoring when that context has ended meanwhile. The guard never infers a context from
  `get_current_tenant_id()` / `get_rls_context_active()`, which are public.
- The library's own GUC writes run inside `writing(alias_or_connection, values)`, which
  keeps the guard out of the way and records `values` (`UNKNOWN` if the block raises). A
  restore records `_get_context_gucs()` read after the `ContextVar` reset
  (`_restore_tenant_gucs()` in `context.py`, which also settles the thread's other
  configured aliases): the guard reconciled the snapshot reads, so the snapshot holds the
  enclosing context's values. A snapshot taken inside `writing()` needs
  `apply_active_context()` first (`_enter_tenant_gucs()`).
- Writes by other code (`set_guc()`, `rls_context()`) are deliberately not recorded.

PostgreSQL undoes `set_config()` calls made in a rolled-back transaction or savepoint, so
the tracker follows Django's commit and rollback (the wrapped `_commit` / `_rollback`), the
libpq transaction status, and the statements in `_FOLLOWED` that run through Django's cursor
(savepoint stack, `ROLLBACK`/`COMMIT` as SQL, `RESET ALL`/`DISCARD ALL`/`RESET` of the two
GUCs; leading comments skipped, compound statements forgotten); what it cannot follow
becomes `UNKNOWN` and is applied again before the next statement. Transaction-control
statements, failed transactions and `needs_rollback` are never reconciled. In tests, a test
that leaves a context open relies on the autouse `_clear_gucs_after_test` fixture, which
calls `_clear_context_gucs()`; a test that checks what a session holds regardless of the
guard reads it with a driver cursor (`tests/session_gucs.py`); a test that counts
statements runs one first, so the guard has settled the connection.

### Strict Mode

When `STRICT_MODE=True`, `TenantQuerySet` evaluation methods raise
`NoTenantContextError` if no RLS context is active. A second `ContextVar`
(`_rls_context_active` in `state.py`) tracks whether `tenant_context()`,
`admin_context()`, `RLSTenantMiddleware`, or `for_user()` has established
context. This distinguishes "no context" (should raise) from "admin context"
(both have `_current_tenant_id=None`). All entry points (including
`TenantQuerySet._rls_scope()`) set the flag to `True` on entry and restore via token on
exit. The guard is in `TenantQuerySet._check_strict_mode()`. A third, private `ContextVar`
(`_context_gucs`) drives the GUC affinity guard (see above).

### `for_user()` Scope

`for_user()` only stores the user. Every `TenantQuerySet` method that runs SQL does so
inside `_rls_scope()` (`for_write=True` for writes, so the router's write alias is used),
which pins that alias on the queryset for the call, sets the GUCs that differ from the
user's values and the tenant `ContextVar`s, then restores the previous values. A new
override that runs SQL must use it. `iterator()` / `aiterator()` enter it once per chunk
and never across a `yield`. A queryset with cached results must skip both the scope and
`_check_strict_mode()`. Methods Django runs in a transaction of their own pass
`own_transaction=True`, so `SET LOCAL` mode works outside `transaction.atomic()`.

### Async Middleware Path

Under ASGI, `RLSTenantMiddleware.__acall__` (not `MiddlewareMixin.__acall__`) serves the
request: one thread-sensitive hop (`_enter_request_gucs`) reads `request.user` and sets the
GUCs, the `ContextVar`s are set and reset with tokens in the request coroutine, and one hop
clears the GUCs. Never set or reset a `ContextVar` inside a `sync_to_async` hop (the token
belongs to a copied context, #58), and never use `thread_sensitive=False` in library code
(another thread means another connection). `process_exception` must keep ending the context
under ASGI, because Django builds the error response on another thread and connection.
Errors Django builds without calling `process_exception` (an unresolvable URL, for one) are
not ended by it: the `connection_created` thread check clears a connection opened for them,
and the GUC affinity guard applies the copied context to their statements and takes it off
the connection before the next statement run without it (#130, #41).
A subclass that overrides `process_request` or `process_response` is detected in `__init__`
and served by `_acall_through_sync_hooks` (`MiddlewareMixin.__acall__`), so changes to the
sync hooks must keep that path working too.

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
├── demo/                Project settings, urls, wsgi/asgi, Celery app (eager mode)
├── tenants/             Tenant model (shared, not RLS-protected)
├── accounts/            Custom User with TenantUser protocol
├── notes/               RLS-protected Note model, views, services, tasks, templates, seed command
└── docker/              PostgreSQL init (creates non-superuser role for RLS)
```

## CI

GitHub Actions runs lint + type-check, a strict mkdocs build (`mkdocs build --strict`),
a test matrix across Python {3.12, 3.13, 3.14} × Django {5.2, 6.0, 6.1} on PostgreSQL 16
(psycopg2, no `exclude` rows) plus one psycopg 3 leg on Python 3.12 / Django 6.1 (the only
one that runs the `psycopg3_only` tests), and a
`test-postgres` job on PostgreSQL 15 and 18 with the oldest and newest supported Django
(5.2 on Python 3.12 / 6.1 on Python 3.14) on every push/PR. Those legs connect as the
`postgres` superuser; a `test-plain-role` job runs the suite once more as a plain role
(PostgreSQL 16, Python 3.12, the locked Django, `-m "not superuser_only"`) after checking
that the connection really is one (see the plain-role rule under Testing). Coverage is
reported from the 3.12 / 6.1 / psycopg2 leg. All PostgreSQL legs set `RLS_FAIL_ON_SKIPPED=1`
(see Testing).
`tests/conftest.py` also turns every Django `RemovedIn*Warning` into a test error, so the
newest Django leg fails as soon as Django deprecates something the library calls.

A weekly **canary** (`.github/workflows/canary.yml`, Mondays 06:00 UTC and on demand) runs
the suite against Django `main`, the next Python and the next PostgreSQL (a beta or RC
image). It blocks nothing; a red run means a matrix review is due. Its versions are
single-value matrix entries with "bump me" comments, and the drift test checks they stay
newer than the supported matrix.

### Supported versions

The policy lives in `docs/getting-started/installation.md` (`## Supported versions`):
every Django series upstream still supports; the Python versions that *all* of them
support; an explicit list of PostgreSQL majors. The matrix is stated in `pyproject.toml`,
`.github/workflows/ci.yml`, `README.md`, `docs/index.md`, the installation guide,
`docs/comparison.md`, this file, both contributing guides and `example/Dockerfile`.
`tests/test_support_matrix.py` parses all of them: change the constants at the top of
that file first, and the test lists every file that disagrees. It reads files the sdist
does not ship, so `pyproject.toml` excludes it from the sdist.

The `example-smoke` job tests the **built wheel**, not the source tree: `uv build`, an
inline check that the wheel holds only `django_rls_tenants/` + `*.dist-info/` (every
package dir has `__init__.py`; `py.typed`, the management commands and `contrib/celery.py`
are present), then `uv pip install dist/*.whl` into a clean `.wheel-venv` with the locked
Django/test deps (`uv export --group test --no-emit-project`). It runs
`example/manage.py check` and `pytest --collect-only` with no database (so `example/` must
keep importing cleanly), then `makemigrations --check --dry-run` and the example suite as
a non-superuser role, all with the venv's interpreter. A new subpackage or data file the
package needs must therefore end up in the wheel. The example migrations are committed, so
a model change in `example/` must ship with a regenerated migration (`cd example && uv run
--project .. python manage.py makemigrations`), or the check step fails.

The release workflow (`publish.yml`) starts with a `preflight` job: the release tag must be
`vX.Y.Z`, equal to `pyproject.toml`'s `version`, and `CHANGELOG.md` must contain a
`## [X.Y.Z]` heading. Make the release commit before creating the GitHub release.
