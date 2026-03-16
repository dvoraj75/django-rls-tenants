# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `_add_tenant_fk` signal handler now reads the configured `TENANT_FK_FIELD` value
  instead of hardcoding `"tenant"` when checking for an existing field. Previously,
  a custom `TENANT_FK_FIELD` (e.g., `"organization"`) would cause the handler to
  miss existing fields and attempt to add a duplicate FK.

### Added

- `get_current_tenant_id()` / `set_current_tenant_id()` /
  `reset_current_tenant_id()` functions for custom middleware and
  management commands that need direct access to the auto-scope state.
  Use the token returned by `set_current_tenant_id()` with
  `reset_current_tenant_id(token)` to safely restore the previous value.

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
  `USE_LOCAL_SET`, `CONN_MAX_AGE_OVERRIDE`.
- `RLSTenantsConfig` singleton with lazy property access and unknown-key
  detection (warns on typos).
- Django system checks: `W001` (GUC prefix mismatch for tenant), `W002`
  (GUC prefix mismatch for admin), `W003` (`USE_LOCAL_SET` without
  `ATOMIC_REQUESTS`), `W004` (`CONN_MAX_AGE > 0` with session-scoped GUCs).

#### User Integration

- `TenantUser` runtime-checkable `Protocol` for structural subtyping of user
  objects. Requires `is_tenant_admin`, `rls_tenant_id`, and `is_authenticated`.

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

[Unreleased]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dvoraj75/django-rls-tenants/releases/tag/v1.0.0
