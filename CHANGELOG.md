# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Actionable error hints** (#34): `RLSTenantError` now accepts a keyword-only
  `hint=` argument and exposes `.message` / `.hint` attributes for programmatic
  access. The `NoTenantContextError` raised by `tenant_context()`,
  `admin_context()`, `_resolve_user_guc_vars()`, the `@with_rls_context`
  decorator, and `STRICT_MODE` now carries a `Hint:` line telling you exactly how
  to fix it, and the `W001`/`W002`/`W008`/`W009` system-check hints name the exact
  setting (and, for `W009`, the missing field). Backward compatible: `str(exc)` is
  unchanged when no hint is supplied.

- **Code of Conduct** (#35): adopted the Contributor Covenant v2.1
  (`CODE_OF_CONDUCT.md`), linked from the contributing guides and the
  documentation. Enforcement contact: dvoraj75@gmail.com.

- **Raw SQL helper `safe_tenant_sql()`** (#33): a `WHERE`-clause fragment that
  scopes raw queries to the current tenant using the same `current_setting()`
  expression as the RLS policies (including the #57 InitPlan form); the GUC
  names and the tenant PK cast come from `RLS_TENANTS`. Supports the same
  `extra_bypass_flags` as `RLSConstraint`, so the fragment can mirror a policy's
  full bypass set. Adds a companion `current_tenant_value_sql()` for `INSERT` /
  `SELECT` value lists. Both contain no bind parameters (the tenant id is read
  in-database from the session GUC), validate every interpolated identifier, are
  re-exported from the top-level package, and are documented in the new Raw SQL
  guide.

- **Native Celery integration** (#32): a new optional `celery` extra
  (`pip install django-rls-tenants[celery]`) adds `django_rls_tenants.contrib.celery`.
  `@rls_task` (a `shared_task` drop-in) and the `RLSTask` base class capture the
  active tenant/admin context into the task's message headers on enqueue and
  restore it on the worker — around the whole body, so it unwinds on both success
  and exception — without passing `tenant_id` by hand. Context propagates across
  chains, groups, and chords (every step must use the integration). Set
  `rls_require_context = True` on an `RLSTask` subclass to fail fast (with the #34
  hint) instead of running unscoped. `install()` / `uninstall()` wire the same
  behaviour globally via Celery signals as an escape hatch for tasks that cannot
  be re-based. Celery stays optional and is **not** re-exported from the
  top-level package; importing the module without Celery raises a clear
  `ImportError`. Synchronous task bodies only (`async def` is out of scope for
  v1.3.0).

### Changed

- **Performance: InitPlan-wrapped GUC reads** (#57): RLS policy predicates now
  read PostgreSQL session variables through an uncorrelated scalar sub-SELECT
  -- `(SELECT current_setting('rls.current_tenant', true))` -- so each GUC is
  evaluated once per statement (a planner *InitPlan*) instead of once per row.
  The predicate is otherwise unchanged, so query results are identical; the win
  shows on large, admin, and raw-SQL scans. Applies to `RLSConstraint`,
  `RLSM2MConstraint`, the `AddM2MRLSPolicy` migration operation, and the
  `setup_m2m_rls` command (which also stops hardcoding `rls.is_admin` /
  `rls.current_tenant` / `int` and instead derives the GUC names and tenant PK
  cast from `RLS_TENANTS`). Existing policies are **not** rewritten
  automatically -- see the migration guide ("From 1.2.2 to 1.3.0") to adopt the
  new form.

## [1.2.2] - 2026-06-27

### Added

- **`setup_m2m_rls --verbose`** (#28): print each M2M through-table policy's
  `CREATE POLICY` SQL before applying it, so DBAs and security reviewers can
  audit the exact SQL while still applying it in one run. Combine with
  `--dry-run` to print without executing.
- **`check_rls --verbose`** (#26): print each policy's live `USING` /
  `WITH CHECK` definition (read from the `pg_policies` view — the SQL
  PostgreSQL actually enforces) under the pass/fail line, for auditing and
  diagnosing unexpected policy behaviour. `--quiet` takes precedence when both
  are given.
- **System checks `W008` + `W009`** (#29): `W008` warns when
  `RLS_TENANTS['TENANT_MODEL']` does not resolve to an installed model;
  `W009` warns when a concrete `RLSProtectedModel` subclass is missing the
  tenant field its RLS policy references (resolved from `RLSConstraint(field=…)`,
  falling back to `TENANT_FK_FIELD`). Both surface the misconfiguration at
  startup via `manage.py check` instead of as a cryptic error at
  migrate/query time.
- **Documentation: Celery quick-start guide** (#30): new Celery Tasks guide
  showing how to wrap task bodies in `tenant_context()` so RLS context is set
  for work that runs outside the request cycle. Interim pattern until native
  Celery integration lands in v1.3.0.

## [1.2.1] - 2026-06-27

### Added

- **DEBUG-level logging** (#24): `logger.debug()` calls in middleware, context
  managers, and M2M auto-detection for tracing the RLS context lifecycle. Uses
  Django's logging framework, so there is zero output unless DEBUG is enabled on
  the `django_rls_tenants` logger.
  - `RLSTenantMiddleware`: logs when GUCs are set (with user type, admin status,
    and tenant ID) and when they are cleared.
  - `tenant_context()` / `admin_context()`: log entry (with tenant ID and
    database alias) and exit.
  - `register_m2m_rls()`: logs skip reasons during M2M auto-detection (explicit
    through model, constraint already exists, unresolved lazy reference, neither
    side protected).
- **`check_rls --quiet`** (#27): suppress success output and report only errors,
  for clean CI/CD pipeline runs. Errors and the non-zero exit code are
  unaffected.
- **`__repr__` on key classes** (#23): added `__repr__()` to `RLSConstraint`,
  `RLSM2MConstraint`, and `RLSTenantsConfig` for readable output in tracebacks,
  shell sessions, and test failures.

### Changed

- **Public API surface cleanup** (#20): Removed internal helpers from
  top-level `__all__` and `_LAZY_IMPORTS` in `__init__.py`. The following
  symbols are no longer re-exported from `django_rls_tenants`:
  - Raw state functions: `get_current_tenant_id`, `set_current_tenant_id`,
    `reset_current_tenant_id`, `get_rls_context_active`,
    `set_rls_context_active`, `reset_rls_context_active`
  - Exception classes: `NoTenantContextError`, `RLSConfigurationError`,
    `RLSTenantError`

  These remain importable from their actual modules
  (`django_rls_tenants.tenants.state` and `django_rls_tenants.exceptions`).
  This guides users toward the safe context manager APIs (`tenant_context`,
  `admin_context`) instead of direct state manipulation.
- **Type annotation completeness for public API** (#25): tightened the
  `@with_rls_context` decorator signature using `ParamSpec` and `TypeVar`, so
  type checkers and IDEs correctly infer the parameter and return types of
  decorated functions instead of collapsing to `Callable[..., Any]`. Verified
  the return annotations on `RLSManager.get_queryset()`, `RLSManager.for_user()`,
  `set_guc()`, `get_guc()`, and `clear_guc()`.
- **Code comment consistency** (#22): added module docstrings to the
  `management` packages, SQL-safety comments on the parameterised `pg_class` /
  `pg_policies` queries in `check_rls`, and explanatory text on the
  `type: ignore` comments in `managers.py`.

### Fixed

- **Documentation accuracy pass** (#21): corrected inaccuracies across
  CHANGELOG, SECURITY.md, README.md, and docs/ pages.
  - SECURITY.md: updated supported versions table to include 1.1.x and 1.2.x.
  - CHANGELOG.md: fixed strict mode PR references from #13 to #14. Corrected
    claim that `for_user()` sets `_rls_context_active` (it satisfies strict
    mode via `_rls_user` instead).
  - README.md: simplified Quick Start config to show only the required
    `TENANT_MODEL` key instead of listing all defaults (which were incomplete).
  - docs/why-rls.md: replaced incorrect `COALESCE`-based policy example with
    the actual `CASE WHEN` / `nullif()` pattern used by `RLSConstraint`. Fixed
    GUC-setting description to distinguish `set_config()` from `SET LOCAL`.

## [1.2.0] - 2026-03-21

### Added

- **M2M join table RLS support** (#11): subquery-based RLS policies for
  auto-generated M2M through tables, ensuring tenant isolation extends to
  many-to-many relationships.
  - `RLSM2MConstraint`: migration-aware `BaseConstraint` that generates
    `EXISTS`-based subquery policies on M2M join tables. Supports
    both-sides-protected, one-side-protected, and self-referential M2M.
  - `AddM2MRLSPolicy`: reversible Django migration operation for applying
    M2M RLS policies. All inputs validated against SQL injection.
  - `register_m2m_rls()`: auto-detection in `AppConfig.ready()` that
    discovers M2M fields on `RLSProtectedModel` subclasses and registers
    `RLSM2MConstraint` on their auto-generated through tables.
  - `setup_m2m_rls` management command for retroactive M2M RLS application
    on existing deployments (with `--dry-run` and `--database` flags).
  - `check_rls` now also verifies RLS on M2M through tables.
- `STRICT_MODE` configuration option. When enabled, `TenantQuerySet` evaluation
  methods (`count()`, `exists()`, `aggregate()`, `update()`, `delete()`,
  `iterator()`, `bulk_create()`, `bulk_update()`, `get()`, `first()`, `last()`,
  and iteration via `_fetch_all()`) raise `NoTenantContextError` if no RLS
  context is active. Off by default. (#14)
- `_rls_context_active` ContextVar in `state.py` with public accessors
  `get_rls_context_active()`, `set_rls_context_active()`, and
  `reset_rls_context_active()`. Tracks whether any RLS context (tenant or admin)
  is active, enabling strict mode to distinguish "no context" from "admin
  context". (#14)
- Custom exception hierarchy in `django_rls_tenants.exceptions`: `RLSTenantError`
  (base), `NoTenantContextError`, `RLSConfigurationError`. All importable from
  the top-level `django_rls_tenants` package. (#12)
- `DATABASES` configuration option for multi-database GUC support. The middleware
  now sets GUCs on all configured database aliases, not just `default`. Default:
  `["default"]` (backward compatible). (#9)
- `connection_created` signal handler that sets GUCs on lazily created database
  connections mid-request (e.g., replica connections opened by a database router).
- `check_rls --database` flag for verifying RLS on non-default databases.
- System checks `W006` (invalid database alias in `DATABASES`) and `W007`
  (`USE_LOCAL_SET=True` without `ATOMIC_REQUESTS` on configured databases).

### Changed

- `tenant_context()`, `admin_context()`, and `RLSTenantMiddleware` now set
  `_rls_context_active=True` on entry and restore the previous value on exit.
  This enables strict mode's "no context" detection. `for_user()` satisfies
  strict mode via the stored `_rls_user` reference. (#14)
- `tenant_context()` and `_resolve_user_guc_vars()` now raise
  `NoTenantContextError` instead of `ValueError` when a non-admin user has
  `rls_tenant_id=None` or when `tenant_id` is `None`.
- `@with_rls_context` decorator now raises `NoTenantContextError` instead of
  `ValueError` when a non-admin user has `rls_tenant_id=None`.
- `RLSTenantsConfig._get()` now raises `RLSConfigurationError` instead of
  `ValueError` when a required config key (e.g., `TENANT_MODEL`) is missing.

## [1.1.0] - 2026-03-17

### Added

- `get_current_tenant_id()` / `set_current_tenant_id()` /
  `reset_current_tenant_id()` functions for custom middleware and
  management commands that need direct access to the auto-scope state.
  Use the token returned by `set_current_tenant_id()` with
  `reset_current_tenant_id(token)` to safely restore the previous value.
- `W005` system check that warns when the default database connection
  uses a PostgreSQL superuser. Superusers bypass all RLS policies,
  completely disabling tenant isolation.

### Changed

- **Automatic query scoping**: `RLSManager.get_queryset()` now adds
  `WHERE tenant_id = X` automatically when a tenant context is active
  (via `tenant_context()`, `admin_context()`, or `RLSTenantMiddleware`).
  This enables PostgreSQL to use composite indexes, eliminating the
  sequential scan penalty of RLS `current_setting()` calls. No code
  changes required -- activates automatically. `for_user()` continues
  to work as before.
- **RLS policy rewrite**: `RLSConstraint` now generates `CASE WHEN`
  policies instead of `OR`-based policies, improving readability and
  clarifying the evaluation structure. Existing policies are updated on
  the next migration. (Note: the primary performance improvement comes
  from auto-scoping above, which enables index usage. The `CASE WHEN`
  rewrite is a clarity improvement, not a performance optimization.)
- `TenantQuerySet.select_related()` now auto-propagates tenant filters
  to joined RLS-protected tables when a tenant context is active.
- Middleware GUC-set tracking now uses `ContextVar` instead of
  `threading.local`, ensuring proper isolation in ASGI (async)
  deployments where multiple coroutines share a single thread.
- Middleware adds `process_exception()` handler that cleans up both
  `ContextVar` state and GUCs when an unhandled view exception prevents
  `process_response` from running.
- `request_finished` safety-net signal handler now also resets the
  `ContextVar` auto-scope state, not just the GUC variables.
- `_resolve_user_guc_vars()` now raises `ValueError` for non-admin
  users with `rls_tenant_id=None` instead of stringifying `None`. This
  catches user-model misconfigurations at middleware/context-manager
  time rather than producing a silent mismatch at the database level.
- `@with_rls_context` now validates `rls_tenant_id` before entering the
  tenant context (fail-fast), providing a clear error message that
  includes the function name.

### Fixed

- `_add_tenant_fk` signal handler now reads the configured `TENANT_FK_FIELD` value
  instead of hardcoding `"tenant"` when checking for an existing field. Previously,
  a custom `TENANT_FK_FIELD` (e.g., `"organization"`) would cause the handler to
  miss existing fields and attempt to add a duplicate FK.
- `W004` system check now correctly detects `CONN_MAX_AGE=None` (Django's
  "keep connections forever" sentinel). Previously, only positive integer
  values were flagged; `None` silently passed the check despite being the
  most dangerous value for GUC leak risk.

## [1.0.0] - 2026-03-15

Initial stable release of django-rls-tenants.

### Added

#### Architecture

- Two-layer architecture with strict import boundary enforced by tests:
  - `rls/` layer: generic PostgreSQL RLS primitives with zero Django model knowledge.
  - `tenants/` layer: opinionated Django multitenancy built on the `rls/` layer.

#### RLS Primitives (`rls/`)

- `set_guc`, `get_guc`, `clear_guc` helpers for managing PostgreSQL session
  variables (GUCs), with regex-based SQL injection prevention.
- `RLSConstraint` for generating `CREATE POLICY` / `DROP POLICY` DDL in
  Django migrations, with support for `int`, `bigint`, and `uuid` primary key types.
- `rls_context` generic context manager for setting/clearing arbitrary GUC variables
  with save/restore nesting support.
- `bypass_flag` context manager for toggling boolean bypass flags within
  a transaction-scoped context.

#### Tenant Models & Managers (`tenants/`)

- `RLSProtectedModel` abstract base class with dynamic tenant foreign key
  added via the `class_prepared` signal. Supports auto-generated and explicit FK
  configurations.
- `TenantQuerySet` with lazy GUC evaluation at query execution time (not
  queryset creation time), solving the lazy evaluation problem for chained queries.
- `RLSManager` with `for_user()` for scoped queries and
  `prepare_tenant_in_model_data()` for efficient bulk creation without N+1
  `SELECT` queries.
- Defense-in-depth: `for_user()` applies both Django ORM `.filter()` and
  database-level RLS, so even if one layer is bypassed the other provides isolation.

#### Middleware & Context

- `RLSTenantMiddleware` for automatic per-request RLS context based on the
  authenticated user. API-agnostic (works with REST, GraphQL, Django views).
- `tenant_context` and `admin_context` context managers with nesting support
  and automatic GUC cleanup via `try/finally`.
- `@with_rls_context` decorator for extracting user context from function
  arguments, with configurable `user_param` and fail-closed behavior.
- `request_finished` signal safety net for GUC cleanup in case middleware's
  `process_response` does not run.

#### Configuration & Validation

- Single `RLS_TENANTS` settings dict with 6 configuration keys:
  `TENANT_MODEL`, `TENANT_FK_FIELD`, `USER_PARAM_NAME`, `GUC_PREFIX`,
  `TENANT_PK_TYPE`, `USE_LOCAL_SET`.
- `RLSTenantsConfig` singleton with lazy property access and unknown-key
  detection (warns on typos).
- Django system checks: `W001` (GUC prefix mismatch for tenant), `W002`
  (GUC prefix mismatch for admin), `W003` (`USE_LOCAL_SET` without
  `ATOMIC_REQUESTS`), `W004` (`CONN_MAX_AGE > 0` with session-scoped GUCs).

#### User Integration

- `TenantUser` runtime-checkable `Protocol` for structural subtyping of user
  objects. Requires `is_tenant_admin` and `rls_tenant_id` properties.

#### Bypass Mode

- `bypass_flag` context manager for temporary bypass of specific RLS policies.
- `set_bypass_flag` / `clear_bypass_flag` imperative helpers.
- `extra_bypass_flags` support on `RLSConstraint` for custom bypass clauses
  (e.g., login flows).

#### Connection Pooling

- `USE_LOCAL_SET` configuration for transaction-scoped GUCs via `SET LOCAL`,
  compatible with PgBouncer and other connection poolers.

#### Management Commands

- `check_rls` management command to verify RLS policies are correctly applied
  to all protected models, with CI-friendly exit codes.

#### Testing Utilities

- `rls_bypass` context manager for disabling RLS in test setup/teardown.
- `rls_as_tenant` context manager for running tests as a specific tenant.
- `assert_rls_enabled` assertion for verifying RLS is active on a table.
- `assert_rls_policy_exists` assertion for verifying a named policy exists.
- `assert_rls_blocks_without_context` assertion for verifying fail-closed behavior.

#### Package & Tooling

- PEP 561 `py.typed` marker for typed package support.
- `__version__` attribute via `importlib.metadata` (single source of truth).
- Full MkDocs Material documentation site with 19 pages: getting started,
  guides, advanced topics, API reference, and development docs.
- CI pipeline: Python {3.11, 3.12, 3.13, 3.14} x Django {4.2, 5.0, 5.1, 5.2, 6.0}
  test matrix with PostgreSQL 16.
- OIDC trusted publishing to PyPI via GitHub Actions.
- Security policy (`SECURITY.md`) with vulnerability disclosure process.

### Fixed

- `clear_guc` now accepts an `is_local` parameter, ensuring consistent GUC
  lifetimes when `USE_LOCAL_SET=True`. Previously, `admin_context`, middleware,
  and manager `_fetch_all` could clear GUCs with session scope while setting
  them with transaction scope, causing mismatched lifetimes.

[Unreleased]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dvoraj75/django-rls-tenants/releases/tag/v1.0.0
