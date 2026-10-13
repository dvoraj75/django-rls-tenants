# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- **Support for Django 4.2, 5.0 and 5.1 and Python 3.11** (#114). All three Django series
  are past their upstream end of extended support, and Python 3.11 is not supported by
  every remaining Django series. Under the new supported-versions policy this is not a
  breaking change: 1.3.x stays on PyPI and keeps working there
  (`pip install "django-rls-tenants<1.4"`).

### Added

- **Async GUC primitives and async context managers** (#36, #37). `aset_guc()`,
  `aget_guc()`, `aclear_guc()` and `arestore_guc()` in `django_rls_tenants.rls`, and
  `async_tenant_context()` and `async_admin_context()` (both also exported top-level),
  `async_rls_context()` and `async_bypass_flag()` for `async with`. Django has no
  asynchronous database cursor, so these are thread-sensitive `sync_to_async` hops, like
  the async ORM itself, not non-blocking calls: the managers make one hop on entry and
  one on exit, set and reset the tenant `ContextVar`s in the calling coroutine (so the
  auto-scope filter and `STRICT_MODE` apply to `acount()` and friends, and the #58 token
  problem cannot occur), and support nesting with sync and async blocks in both GUC
  modes, including restore when the task is cancelled (even during entry). With `USE_LOCAL_SET=True` the tenant
  managers raise `RLSConfigurationError` unless the coroutine runs via `async_to_sync`
  inside a synchronous `transaction.atomic()`. Use one context per task: sibling tasks
  of one request share its connection, so `asyncio.gather` across tenants is not safe
  for raw SQL. No behaviour change for sync code.
- **Django 6.1 support** (#114), with classifiers and CI legs.
- **Explicit PostgreSQL 15–18 support** (#114). The requirements tables list every
  supported major; CI runs 16 by default and 15 and 18 as the edges.
- **psycopg 3 CI leg** (#114) on Python 3.12 / Django 6.1. The library stays
  driver-neutral; psycopg2 remains the default in every other leg.
- **Supported-versions policy** in the installation guide ("Supported versions", #114):
  which Django, Python and PostgreSQL versions are supported, when they are added and
  dropped, and what dropping means.
- **`tests/test_support_matrix.py`** (#114) cross-checks every file that states the
  matrix (`pyproject.toml`, CI, README, docs, AGENTS.md, both contributing guides, the
  example Dockerfile) and names each one that disagrees. Excluded from the sdist, since
  it reads files the sdist does not ship. The test suite also turns every Django
  deprecation warning into an error.
- **Weekly canary workflow** (`.github/workflows/canary.yml`, #114): Django `main`, the
  next Python (3.15) and the next PostgreSQL (19 beta). It blocks nothing; a red run
  means a matrix review is due.

### Changed

- `requires-python` is now `>=3.12` and the dependency `django>=5.2` (#114). The lockfile
  moves to Django 6.1.2 (django-stubs 6.1.2, mypy 2.4.0) and psycopg2-binary 2.9.13 (the
  first release with Python 3.15 wheels); the example Docker image installs Django 6.1.
- **`@with_rls_context` rejects generator functions and coroutine results** (#39).
  Decorating a generator or async generator function now raises `TypeError` at
  decoration time: its body runs while the caller iterates, after the decorator has left
  the context, so it never ran under the decorated user's context. A function decorated
  as synchronous that returns a coroutine (an object with `async def __call__`, or an
  `async def` under a decorator that hides it) now raises `TypeError` when called, after
  closing the coroutine. Enter the context inside the generator or coroutine instead.

### Fixed

- **`select_related()` with no arguments on Django 6.1** (#114). Django 6.1 deprecates
  that form, and the `RemovedInDjango2028Warning` blamed
  `TenantQuerySet.select_related` in `managers.py` instead of the code that made the
  call. The warning now names the caller's line, and Django-internal callers (the admin
  changelist with `list_select_related = True`, which Django deprecates separately) are
  not warned twice. No warning on Django 5.2 or 6.0, where the form is not deprecated.
- **`MiddlewareMixin` import path** (#114). Django 6.2 moves `MiddlewareMixin` to
  `django.middleware` and deprecates importing it from `django.utils.deprecation`
  (`RemovedInDjango2029Warning`). `RLSTenantMiddleware` now imports from the new location
  when it exists and falls back to the old one on Django 5.2–6.1, so the warning never
  points at this library.
- **`@with_rls_context` on `async def` functions** (#39). The decorator now returns an
  `async def` wrapper for coroutine functions, which enters `async_tenant_context()` /
  `async_admin_context()` around the awaited body. Before, the sync wrapper entered and
  left the context before the coroutine ran, so the body ran under the caller's context:
  called from synchronous code inside `admin_context()` (for example via
  `async_to_sync`), a coroutine decorated for a tenant user saw every tenant's rows, and
  awaited from async code the call failed with `SynchronousOnlyOperation`.

## [1.3.4] - 2026-10-12

### Fixed

- **`RLSTenantMiddleware` crashed with `Token was created in a different Context` under
  ASGI** (#58). Django runs a sync middleware's hooks through `MiddlewareMixin.__acall__`,
  which calls `process_request` and `process_response` / `process_exception` in separate
  `sync_to_async` hops, each in its own copy of the request's `contextvars.Context`. The
  tenant state reached the view, but the tokens saved by `process_request` belonged to a
  discarded context, so every authenticated ASGI request ended in a 500 (a view exception
  was masked by the same `ValueError`), and the GUC clear after the failed reset was
  skipped. Cleanup now clears the `ContextVar`s to their defaults when `reset()` rejects a
  token from another context (logged at `DEBUG`), still resets via the token on WSGI, and
  clears the GUCs in a `finally` block so a reset failure can never skip them. A token
  reused twice still raises. Cleanup also runs when an async view is cancelled and
  Django skips the response and exception hooks. `USE_LOCAL_SET=True` remains WSGI-only.

## [1.3.3] - 2026-10-12

### Added

- **Troubleshooting page and task-oriented index** (`docs/troubleshooting.md`, #109). One
  symptom / cause / fix entry per known failure mode, each linking to the guide that owns
  the explanation: zero rows everywhere, the three `NoTenantContextError` hints, `SET LOCAL`
  outside a transaction, the `USE_LOCAL_SET` middleware-order and ASGI configuration
  errors, every active system check (W001/W002, W004, W005, W006, W008, W009) with the
  retired W003/W007, `check_rls` findings, the `createsuperuser` admin trap, Celery
  propagation, PgBouncer GUC leaks, raw SQL scoping, planner behaviour, the open ASGI
  token bug (#58), and the 1.3.2 fixes (#79, #94, #95, #100, #102) as "upgrade to fix".
  Covers the older exception-import and token-reuse failures too, and distinguishes
  streaming transaction requirements, Celery enqueue-time context, and indexable raw
  SQL predicates from the admin-inclusive form.
  The docs home page gains an "I want to..." table mapping fifteen common goals to the
  page that covers them. Both are in the site nav; the README and the Quick Start's
  "What's Next?" point at the troubleshooting page.

- **Example demos for `@rls_task`, `safe_tenant_sql()` and `check_rls --verbose`** (#80).
  The example now ships a broker-free Celery app (`demo/celery.py`, eager mode) and
  `notes/tasks.py` with a plain `@rls_task` driven from the Stats page plus a
  `rls_require_context = True` task that fails fast without a context; `get_note_stats`
  adds a raw aggregate scoped with `safe_tenant_sql()` that the page shows next to the
  ORM figure it equals for tenant users and admins alike. `tests/test_tasks.py` and
  `tests/test_services.py` run both round-trips against a non-superuser role in CI.
  The README walks through `check_rls --verbose` output and `manage.py check`
  (W005 / W008 / W009), and the Celery and Raw SQL guides point at the example.

- **Performance & Indexing guide** (`docs/guides/performance.md`, #83). One page for the
  "what does RLS cost and how do I index for it" question, built from measured
  `EXPLAIN (ANALYZE, BUFFERS)` plans on a 1,000,000-row table as a non-superuser role:
  why the policy is always a post-scan `Filter` while the ORM's `WHERE tenant_id = X`
  becomes the `Index Cond`, composite `(tenant, ...)` indexes via `Meta.indexes`,
  partial-index interaction, the v1.3.0 InitPlan form side by side with the inline
  form, `select_related()` / `prefetch_related()` / M2M join plans, which
  `safe_tenant_sql()` form is indexable, how to measure, and a checklist. Linked from
  the README, the home page, Protecting Models, Raw SQL, Architecture and Security Model.

### Changed

- Docs no longer claim the planner skips the policy because `current_setting()` is not
  `LEAKPROOF`. Measured plans show a bare `tenant_id = current_setting(...)::int`
  policy *is* used as an index condition; what makes the library's policy a post-scan
  filter is its `CASE WHEN <admin> THEN true ELSE ... END` shape (and an `OR`-split
  into two permissive policies is no better). `protecting-models.md` and the new guide
  state the real cause. The Raw SQL guide now says that only the
  `safe_tenant_sql(include_admin=False)` fragment (or a bind parameter) reaches the
  index; the admin-inclusive default plans as a sequential scan like the policy. (#83)
- **Test-coverage backfill for v1.3.0 code paths** (#84). Regression guards for wiring
  that the existing suite would not have caught if removed: the three error-hint
  constants are pinned verbatim (`tests/test_tenants/test_errors.py`); `check_rls_config()`
  is tested at the orchestrator level (every `_check_*` helper is aggregated, and W006 /
  W008 / W009 surface from a broken config); `RLSTenantModelAdmin.save_model` tenant-id
  normalisation is pinned DB-free for int and UUID primary keys; custom
  `rls_session_key` / `rls_tenant_query_param` names round-trip through selection sync,
  effective-tenant resolution, the view context and the switcher filter; and Celery
  canvas propagation (chain / group / chord) checks admin headers and context at every
  step under `admin_context()`, plus the current-task fallback with a tenant header but
  no admin key. No library code changed.
- **Example app: migrations are committed and `.env.example` is gone** (#81). The three
  example apps now ship `0001_initial.py` instead of relying on a runtime
  `makemigrations` in `docker-compose.yml`, the README and CI. The notes migration
  carries the `AddConstraint(RLSConstraint(...))` operations that emit the RLS policy DDL,
  which is the most instructive artifact of the demo and was previously only ever
  generated inside the container. `migrate` alone now brings up the policies, and the CI
  example job runs `makemigrations --check --dry-run` so model/migration drift fails the
  build. The orphaned `example/.env.example` is deleted: nothing read it (compose injects
  `DATABASE_URL` and the README's local-development section uses `export`).
- **CI now proves what the README claims** (#110). A `test-postgres` job runs the suite on
  PostgreSQL 15 and 17 with the oldest and newest supported Django (4.2 / 6.0) alongside
  the PostgreSQL 16 matrix, so "PostgreSQL >= 15" is verified at both ends. Both
  matrices explicitly select and verify Python/Django versions and disable re-syncing
  when running pytest, preserving the requested Django version. Every
  PostgreSQL leg sets `RLS_FAIL_ON_SKIPPED=1`: a `pytest_sessionfinish` hook in
  `tests/conftest.py` (mirrored in the example suite) fails the session on any skipped
  test, and the one runtime `pytest.skip()` in the suite is now a registered
  `superuser_only` marker that fails on a plain role and is deselected with
  `-m "not superuser_only"`. The example job builds the wheel with `uv build`, asserts it
  contains only `django_rls_tenants/` and `*.dist-info/` (with `py.typed`, an
  `__init__.py` in every package directory, the management commands and
  `contrib.celery`), installs it into a clean virtualenv at the locked Django version and
  runs `manage.py check`, `makemigrations --check` and the example tests against the
  installed distribution instead of the source tree. The release workflow gains a
  `preflight` job that refuses a tag whose `vX.Y.Z` does not equal `pyproject.toml`'s
  version or has no `## [X.Y.Z]` heading in this file. No library code changed.

### Fixed

- **Docs: the Quick Start and User Integration guides no longer make `User`
  RLS-protected** (#82). `class User(AbstractUser, RLSProtectedModel)` silently
  registers `RLSM2MConstraint` on the inherited `groups` and `user_permissions` through
  tables, so `check_rls` lists them under an extra `M2M through tables:` section and
  reports 4 tables, not the 2 the Quick Start promised. Worse, the snippet declared no
  `Meta`, so `AbstractUser.Meta` won and the user table itself had no `RLSConstraint`:
  `check_rls` failed on the missing policy instead of printing the documented output.
  Both guides
  now use a plain `AbstractUser` with a nullable `tenant` FK (the shape the example
  project and test app use), the Quick Start output matches a real run, and a new
  *Protecting the User Model* section in the Protecting Models guide documents the
  opt-in case: the auth through tables get RLS, `RLSM2MConstraint` has no
  `extra_bypass_flags`, and fresh group/permission queries need a tenant or admin
  context. Protected auth users explicitly inherit the RLS `Meta` constraint, the
  authentication backend covers session reloads via `get_user()`, and the guide notes
  Django's permission cache and superuser behavior.
- **Docs: the roadmap is reachable from the site nav** (#82). `development/roadmap.md`
  was committed but missing from the Development section of `mkdocs.yml`. It is now
  listed after the changelog and gains a `v1.3.x — Patch cycle` entry so the public
  roadmap no longer jumps from v1.3.0 to v1.4.0.

## [1.3.2] - 2026-10-11

### Fixed

- **`override_settings(RLS_TENANTS=...)` was silently ignored** (#100).
  `RLSTenantsConfig` cached `settings.RLS_TENANTS` on first read and never
  invalidated it, so `override_settings` (context manager or decorator),
  `SimpleTestCase.settings()` and pytest-django's `settings` fixture had no effect on
  the library once any setting had been read: a test flipping `STRICT_MODE` or
  `USE_LOCAL_SET` ran with the original value and passed for the wrong reason. The app
  config now connects Django's `setting_changed` signal and reloads the config whenever
  `RLS_TENANTS` changes (the unknown-key typo check re-runs too). A public
  `rls_tenants_config.reload()` exposes the same hook. The library's own tests and the
  example suite drop their manual `_config_cache = None` workarounds.
- **`select_related()` silently dropped rows on nested nullable paths** (#79).
  `TenantQuerySet.select_related("a__b")` adds a tenant predicate on the joined
  RLS-protected table, and for nullable relations it ORs in `a__b__isnull=True` so the
  join stays a LEFT OUTER JOIN. The nullability check looked up the *whole dotted path*
  with `get_field()`, which never resolves for multi-hop paths, so no `isnull` clause was
  added and the predicate forced an INNER JOIN: rows whose chain had a `NULL` at any hop
  (a missing intermediate object or a missing leaf) vanished from the result. The path
  is now walked segment by segment and the `isnull` escape hatch is added when **any**
  hop is nullable. Fully non-null chains keep their INNER JOIN. Single-hop behaviour
  is unchanged.
- **Nested context managers now restore the outer database scope under
  `USE_LOCAL_SET=True`** (#94). `tenant_context()`, `admin_context()`, `rls_context()`
  and `bypass_flag()` skipped the GUC save/restore in `SET LOCAL` mode and relied on the
  transaction end, so an inner block's scope outlived the block: after an inner
  `admin_context()` exited, the connection kept `rls.is_admin = 'true'` for the rest of
  the transaction while `get_current_tenant_id()` still reported the outer tenant. Raw
  SQL, `update()` / `delete()` on plain managers and models without `RLSManager` saw
  every tenant. All four context managers now read the previous values on entry and
  re-issue them with the same scope on exit, in both GUC modes. New
  `django_rls_tenants.rls.guc.restore_guc()` and `rls.context.restore_gucs()` are the
  shared primitives. `rls_context()` also restores earlier settings when a later
  setting fails during entry, preventing a partially applied admin bypass from leaking
  in autocommit mode.
- **Context managers no longer mask the real database error** (#102). Exiting a block
  after a failed statement (`IntegrityError`, `DataError`, ...) ran the GUC restore on an
  aborted transaction, and the resulting `InternalError: current transaction is aborted`
  replaced the original exception (default `set_config` mode; `SET LOCAL` mode was only
  unaffected because of #94). When the block exits with a pending exception, a
  `DatabaseError` raised by the restore itself is now logged at DEBUG and swallowed; the
  transaction or savepoint rollback restores the GUCs. A clean exit still propagates
  restore failures.
- **`RLSTenantMiddleware` crashed on every authenticated request with
  `USE_LOCAL_SET=True`** (#95), including the `ATOMIC_REQUESTS=True` setup the
  connection-pooling guide recommended. Django's `ATOMIC_REQUESTS` wraps only the view
  callable, so `process_request` ran in autocommit and `SET LOCAL` raised
  `RuntimeError: Cannot use SET LOCAL ... outside a transaction`. The middleware now
  owns the transaction when `USE_LOCAL_SET=True`: for authenticated requests it opens
  `transaction.atomic()` on every alias in `RLS_TENANTS["DATABASES"]` around the rest of
  the chain and the view, so the GUCs cover the whole request and vanish at
  commit/rollback. `ATOMIC_REQUESTS` is no longer required (it nests as a savepoint if
  kept); the default `USE_LOCAL_SET=False` path is unchanged.
    - A view that raises rolls the request transaction back; an explicit error response
      commits. The rollback is applied when the transaction exits, not in
      `process_exception`, so error handlers can still query after a non-database
      exception such as `Http404` (their writes roll back too). An exception that
      escapes the middleware chain directly still cleans up the request state.
    - Exception-handling middleware, and function-based middleware factories, must be
      listed *before* `RLSTenantMiddleware`: Django runs `process_exception` hooks in
      reverse order, and a later handler that returns a response would skip the
      rollback hook and commit partial writes. Unsafe ordering raises
      `RLSConfigurationError` when the middleware is loaded.
    - Under ASGI the mode is refused with a clear `RLSConfigurationError` instead of the
      opaque `RuntimeError` (native async support is v1.4.0). Async detection no longer
      relies on `MiddlewareMixin.async_mode`, which Django 4.2 and 5.0 do not have.
    - System check `W004` no longer recommends `ATOMIC_REQUESTS`.
    - New end-to-end tests drive `django.test.Client` through the real middleware stack
      with and without `ATOMIC_REQUESTS`, including database-backed error handlers,
      handled view failures, unsafe middleware ordering and raw-SQL tenant isolation.

### Removed

- System check `W007` (`USE_LOCAL_SET=True` without `ATOMIC_REQUESTS`) is retired (#95).
  The combination it warned about is now valid, because the middleware opens its own
  transaction. Check IDs are not reused; leaving `django_rls_tenants.W007` in
  `SILENCED_SYSTEM_CHECKS` is harmless.

## [1.3.1] - 2026-10-11

### Added

- `clear_bypass_flag()` now accepts `is_local=True`, mirroring `set_bypass_flag()`, so a
  transaction-scoped (`SET LOCAL`) bypass flag can be cleared the same way it was set.
  Keyword-only, default `False`; no existing call changes. (#76)

### Changed

- **Example admin now showcases `RLSTenantModelAdmin`** (#73). `example/notes/admin.py`
  registers `Note` and `Category` with the v1.3.0 mixin instead of a plain
  `admin.ModelAdmin` with a hand-rolled `tenant` list filter. `seed_demo` adds a
  tenant-scoped staff account (`staff` / `staff1234`: Acme Corp, `is_staff=True`,
  `is_tenant_admin=False`, permissions on the notes app) so the auto-scoped changelist and
  the hidden + auto-assigned tenant FK can be seen at `/admin/`; the existing `admin`
  account demonstrates the tenant switcher. New `example/tests/test_admin.py` drives both
  paths through the test client. Existing demo volumes gain the missing staff account
  when the seed command runs again, without resetting their data.
- CI now runs `mkdocs build --strict` on every pull request (#77), so broken
  links and stale `mkdocstrings` references fail before merge instead of
  after. Deploy is still only on `main`.
- Pre-commit ruff hooks now run `uv run ruff` (local hooks, like mypy) so
  they always use the ruff version pinned in `uv.lock` -- the same one CI
  runs (#77). Previously the hook was pinned independently (`v0.15.5` vs
  `0.15.20` in the lockfile) and local autofix/format could disagree with CI.
- System check `W005` now also flags a database role with the `BYPASSRLS` attribute,
  which bypasses every RLS policy exactly like a superuser. It reads `rolsuper` and
  `rolbypassrls` from `pg_roles` (previously only `pg_user.usesuper`), names the role and
  the offending attribute in the message, and recommends `NOSUPERUSER NOBYPASSRLS` in the
  hint (with a ready-to-run, identifier-quoted `ALTER ROLE` for the current role). Projects
  already connecting as a plain role see no change. (#99)

### Removed

- System check `W003` (`USE_LOCAL_SET` without `ATOMIC_REQUESTS` on `default`). It only
  looked at `DATABASES['default']`, duplicated `W007` on single-database projects and
  fired spuriously when `default` is not an RLS-managed alias. `W007` covers every
  configured alias; nothing to do unless you reference `W003` in a custom check, and
  silencing it via `SILENCED_SYSTEM_CHECKS` remains harmless. (#76)

### Fixed

- The README and the docs landing page now list the v1.3.0 features (tenant-aware
  admin, native Celery integration, raw SQL helpers) with links to their guides, and
  `SECURITY.md` "Supported Versions" now includes the current `1.3.x` series. (#75)
- Middleware cleanup now resets context tokens only once when Django calls both
  exception and response hooks, preserving the expected 403 for denied admin pages.
- **Example app crashed at import** (#72): `example/notes/views.py` and
  `example/tests/test_rls.py` imported `NoTenantContextError` from the package root,
  which v1.2.1 intentionally stopped exporting. Both now use the canonical
  `from django_rls_tenants.exceptions import NoTenantContextError`. A new
  `example-smoke` CI job runs the example's `manage.py check` (and test collection)
  without a database so an import-level break in `example/` can no longer ship.
- `RLSM2MConstraint` docstring said FK references are verified via `IN (SELECT ...)`;
  the generated policies use `EXISTS (SELECT 1 ...)`. Docstring now matches the DDL. (#76)
- `docs/guides/connection-pooling.md` now quotes the real `W007`/`W004` check messages
  and hints instead of approximate text. (#76)
- **Example README** (#74): the "Database-level proof" step told you to run
  `manage.py dbshell` inside the `web` container, which has no `psql` and fails with
  `CommandError`. It now connects through the `db` container as the non-superuser `app`
  role, with complete host-terminal commands for both the `app` and `demo` superuser
  queries. The expected 15-note total is qualified as a freshly seeded database.
  The features table and project tree also list `tests/test_seed_demo.py`.
- **Security guide threat model** (#99): "Tenant impersonation via SQL" was listed as
  protected against, but any SQL running on the application connection (an injection,
  compromised code, a `dbshell` with the app credentials) can call `set_config()` to
  change the tenant or admin GUC. The guide now scopes the guarantee to *omitted tenant
  scoping*, moves arbitrary SQL execution to "Not Protected Against", and explains that
  PostgreSQL exempts referential-integrity checks and unique indexes from RLS, so FK
  targets and per-tenant uniqueness need their own validation. The architecture page no
  longer claims `FORCE ROW LEVEL SECURITY` stops superuser bypass.

## [1.3.0] - 2026-06-28

### Added

- **Tenant-aware Django admin** (#31): a new `RLSTenantModelAdmin` mixin (subclass
  it instead of `admin.ModelAdmin`) runs every admin DB operation inside the RLS
  context implied by the logged-in user. It wraps the changelist/add/change/
  delete/history views so the active context spans lazy querysets, forms, related
  dropdowns, and saves; changelists and related dropdowns are scoped via the
  existing manager auto-scoping (no `get_queryset` override). The tenant FK is
  hidden from the form and from any explicit `fieldsets` layout, and auto-assigned
  when the tenant is implicit; cross-tenant admins get a
  session-backed tenant switcher (a changelist filter), and a non-admin user with
  no tenant is denied with `PermissionDenied` (carrying the #34 hint) rather than
  silently elevated. Behaviour is configured with class attributes only — no new
  `RLS_TENANTS` keys. `RLSTenantModelAdmin` is re-exported from the top-level
  package. Pair it with `RLSTenantMiddleware` so site-level admin views
  (e.g. autocomplete) are scoped too. Sync only.

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

[Unreleased]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.4...HEAD
[1.3.4]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.3...v1.3.4
[1.3.3]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.2...v1.3.0
[1.2.2]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/dvoraj75/django-rls-tenants/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dvoraj75/django-rls-tenants/releases/tag/v1.0.0
