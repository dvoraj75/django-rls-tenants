# Roadmap

> Last updated: 2026-10-16
>
> Priorities may shift based on community feedback.
> Have a suggestion? [Open an issue](https://github.com/dvoraj75/django-rls-tenants/issues).

**Status markers:** `[ ]` Planned | `[~]` In Progress | `[x]` Done

---

## v1.2.1 — Housekeeping & Debuggability

Quick wins that improve the debugging and development experience.

- [x] **Fix: `TENANT_FK_FIELD` signal handler** — The `_add_tenant_fk` handler
  hardcodes `"tenant"` when checking whether a model already defines the FK
  field, but uses the configured `TENANT_FK_FIELD` when adding it. This causes
  duplicate FK attempts when a custom field name is configured and the model
  already defines that field.
  *Why:* Real bug affecting anyone using a custom `TENANT_FK_FIELD` name.

- [x] **Public API surface cleanup** — Remove internal helpers from `__all__`
  and `_LAZY_IMPORTS`: raw state functions (`get_current_tenant_id`,
  `set_current_tenant_id`, `get_rls_context_active`, etc.) and exception
  classes (`NoTenantContextError`, `RLSConfigurationError`, `RLSTenantError`).
  Keep them importable from their actual modules but stop re-exporting from
  the top-level package.
  *Why:* These are internal implementation details. Exporting them encourages
  direct state manipulation instead of using the safe context manager APIs.

- [x] **Docs accuracy pass** — Correct inaccuracies in CHANGELOG, SECURITY.md,
  and README that reference features differently from the actual implementation.
  *Why:* Misleading docs erode trust and cause integration mistakes.

- [x] **Code comment consistency** — Add module docstrings to management package
  `__init__.py` files, safety comments on f-string SQL in `check_rls` command,
  and explanatory text to `type: ignore` comments in `managers.py`.
  *Why:* Makes the codebase easier to audit and contribute to.

- [x] **`__repr__` on key classes** — Add `__repr__()` to `RLSConstraint`,
  `RLSM2MConstraint`, and `RLSTenantsConfig` for better output in tracebacks,
  Django shell sessions, and test failures.
  *Why:* These objects currently show as `<RLSConstraint object>` which tells
  you nothing when debugging migration or configuration issues.

- [x] **DEBUG-level logging** — Add `logger.debug()` calls to middleware (GUC
  set/clear), `tenant_context`/`admin_context` (entry/exit), and
  `register_m2m_rls()` (skip reasons). Uses Django's logging framework — zero
  output unless the user enables DEBUG.
  *Why:* When things go wrong, there is no trace of what RLS context was set
  or why a model was skipped during M2M auto-detection.

- [x] **Type annotation completeness** — Add missing return type annotations on
  `RLSManager.get_queryset()`, `RLSManager.for_user()`, `set_guc()`, and
  tighten the `@with_rls_context` decorator signature.
  *Why:* Improves IDE auto-complete and type checker support for library users.

---

## v1.2.2 — Management Commands & System Checks

Better CLI tooling and earlier misconfiguration detection.

- [x] **`check_rls --verbose`** — Show full RLS policy SQL for each table, not
  just pass/fail status.
  *Why:* When policies exist but behave unexpectedly, seeing the actual SQL
  is the fastest way to diagnose the issue.

- [x] **`check_rls --quiet`** — Suppress success output, only show errors.
  *Why:* CI/CD pipelines need clean output — success should be silent.
  (shipped in v1.2.1)

- [x] **`setup_m2m_rls --verbose`** — Show the generated SQL before execution.
  *Why:* DBAs and security reviewers need to audit what SQL will run before
  approving it.

- [x] **New system checks** — `W008`: verify `TENANT_MODEL` resolves to an
  installed model at startup; `W009`: verify `TENANT_FK_FIELD` exists on all
  `RLSProtectedModel` subclasses.
  *Why:* These misconfigurations currently fail at query time with cryptic
  errors instead of being caught at startup.

- [x] **Documentation: Celery quick-start** — Minimal pattern for wrapping Celery
  tasks with `tenant_context()` before full Celery integration lands in v1.3.0.
  *Why:* Celery is the most common question from new users — a docs page
  unblocks them immediately without waiting for library-level support.

---

## v1.3.0 — Django Admin & Developer Experience

Highest adoption impact, no new dependencies.

- [x] **Tenant-aware Django Admin** — `RLSTenantModelAdmin` mixin that auto-sets
  tenant context from the admin user, hides the tenant FK field on forms,
  scopes changelists to the current tenant, and adds a tenant filter dropdown
  for admin users.
  *Why:* Every Django project uses the admin — tenant-unaware admin is a daily
  friction point.

- [x] **Celery task integration** — `@rls_task` decorator and `RLSTask` base class
  that serialize tenant context into Celery task headers and restore it on the
  worker side. Supports task chains and groups.
  *Why:* Background tasks without tenant context are a data-leak risk. Users
  currently must manually wrap every task body in `tenant_context()`.

- [x] **Raw SQL helpers** — `safe_tenant_sql()` utility for injecting
  `current_setting()` calls into raw SQL strings safely.
  *Why:* Raw SQL is sometimes unavoidable (reports, bulk operations), and
  there is no safe, documented pattern for it today.

- [x] **Better error messages** — Add solution suggestions to
  `NoTenantContextError` (e.g., "did you forget to wrap this in
  `tenant_context()`?") and make system check hints more actionable.
  *Why:* Reduces time-to-fix for new users hitting common misconfiguration.

- [x] **CODE_OF_CONDUCT.md** — Adopt the Contributor Covenant.
  *Why:* Standard open-source practice that signals project maturity and
  welcoming community.

---

## v1.3.x — Patch cycle

No new library features; each patch tightens what v1.3.0 shipped. Details for every
item are in the [changelog](changelog.md).

- [x] **1.3.1 — Example project and docs front door** — the `example/` admin
  showcases `RLSTenantModelAdmin`, the README and docs landing page list the v1.3.0
  features, `W005` also flags `BYPASSRLS` roles, and CI builds the docs with
  `mkdocs build --strict`. See [1.3.1](changelog.md#131-2026-10-11).
  *Why:* The first thing a new user opens must match what the library does.

- [x] **1.3.2 — Bug fixes** — `override_settings(RLS_TENANTS=...)` is honoured (#100),
  nested-nullable `select_related()` keeps its rows (#79), nested context managers
  restore the outer scope and preserve database errors (#94, #102), and the middleware
  owns the `SET LOCAL` transaction and the request rollback (#95).
  See [1.3.2](changelog.md#132-2026-10-11).
  *Why:* Each one let a test or a request pass for the wrong reason.

- [x] **1.3.3 — Demos, docs accuracy, test backfill, CI gates** — committed example
  migrations and a Celery demo (#80, #81), docs accuracy fixes and this roadmap in the
  site nav (#82), a performance guide (#83), a troubleshooting page (#109), v1.3.0 test
  backfill (#84) and a PostgreSQL matrix with release gates in CI (#110).
  See [1.3.3](changelog.md#133-2026-10-12).
  *Why:* Closes the gap between what is documented and what is verified.

- [x] **1.3.4 — ASGI middleware fix** — `RLSTenantMiddleware` no longer fails every
  authenticated ASGI request with `Token was created in a different Context`, never
  skips the GUC clear, and cleans up cancelled requests (#58).
  See [1.3.4](changelog.md#134-2026-10-12).
  *Why:* The sync middleware must work on async deployments until the native async
  path lands in v1.4.0.

---

## v1.4.0 — Async Support

First-class support for async Django (ASGI, async views, async ORM).

Tenant state already lived in `contextvars.ContextVar`s, the right primitive for async,
but every public API that touches the database (GUC operations, context managers,
middleware, decorator) was synchronous. Django has no asynchronous database cursor: every
async ORM call is a `sync_to_async(..., thread_sensitive=True)` hop, and under ASGI all
such hops of one request run on one thread, and so on one connection. The async APIs below
therefore make thread hops too; what they add is `async with` ergonomics, and tenant state
set and reset in the calling coroutine, in one `contextvars.Context` (#58). The real
connection-affinity problems are code on *other* threads (`thread_sensitive=False` hops,
the error pages Django builds on a worker thread; #130, #41), sibling tasks sharing one
connection (#41), and pooled connections that keep a previous borrower's GUCs (#96).

This release addresses the full async stack, from low-level GUC operations
up through middleware and context managers, and the connection lifecycle under it.
See [1.4.0](changelog.md#140-2026-10-16).

### Tier 0: Platform

- [x] **Supported-versions policy and matrix refresh** (#114) — A written policy (every
  Django series upstream supports; the Python versions all of them support; an explicit
  PostgreSQL list), applied: drop Django 4.2/5.0/5.1 and Python 3.11, add Django 6.1,
  PostgreSQL 15–18 with CI on 15 and 18, a psycopg 3 CI leg. On Django 6.1 the
  `select_related()`-without-arguments deprecation names the caller.
  `tests/test_support_matrix.py` fails when any file states a different matrix, and a
  weekly canary runs Django `main`, the next Python and the next PostgreSQL.
  *Why:* The matrix had drifted (EOL Django series, a coverage gate pinned to one, an
  example image on 5.1), and nothing would have noticed the next drift.
- [x] **`for_user()` applies the user's context to every SQL method, and a plain-role CI
  leg** (#131) — `count()`, `exists()`, `aggregate()`, `iterator()`, the writes and their
  async variants ran without the GUCs. Each now runs in a scope that sets them on the
  alias the SQL uses and restores the previous values afterwards. A new CI job runs the
  library suite as a non-superuser role.
  *Why:* Every other CI leg connects as a superuser, which bypasses RLS, so the bug
  passed every test.

### Tier 1: Async GUC primitives (`rls/guc.py`)

- [x] **`aset_guc()`, `aget_guc()`, `aclear_guc()`, `arestore_guc()`** — Async
  counterparts to the existing sync functions (#36). Each runs the sync function in
  one `sync_to_async(..., thread_sensitive=True)` hop: Django has no asynchronous
  database cursor (every async ORM call is the same kind of hop), so these are not
  non-blocking, but they run on the thread that owns the task's connection.
  *Why:* `await`-able building blocks for async code that manages GUCs directly. The
  win is ergonomics and the correct connection, not a non-blocking database call.

### Tier 2: Async context managers (`tenants/context.py`, `rls/context.py`)

- [x] **`async_tenant_context()`** — `@asynccontextmanager` variant of
  `tenant_context()` (#37). One thread hop on entry (snapshot and set the GUCs) and
  one on exit (restore), instead of one per GUC call; the tenant `ContextVar`s are set
  and reset in the coroutine, so the async ORM is scoped and strict mode applies.
  *Why:* `tenant_context()` uses `@contextmanager` (sync generator) and
  cannot be used with `async with`. Wrapping it in `sync_to_async` breaks
  the generator protocol.

- [x] **`async_admin_context()`** — `@asynccontextmanager` variant of
  `admin_context()` (#37).
  *Why:* Same issue as `tenant_context()` — sync generators are not
  async-compatible.

- [x] **`async_rls_context()` and `async_bypass_flag()`** — Async variants
  of the generic `rls_context()` and `bypass_flag()` context managers.
  *Why:* Completes the async context manager surface area so users never
  need to manually bridge sync/async for RLS operations.

### Tier 3: Async middleware (`tenants/middleware.py`)

- [x] **Async-native `RLSTenantMiddleware`** — A hand-written `__acall__` replaces
  `MiddlewareMixin.__acall__` under ASGI (#38). One thread-sensitive hop reads
  `request.user` and sets the GUCs, the tenant `ContextVar`s are set and reset with their
  own tokens in the request coroutine, and one hop clears the GUCs. Django has no async
  cursor, so the GUC work still needs those hops.
  *Why:* `MiddlewareMixin.__acall__` ran the hooks in separate hops, each in its own copy
  of the request context, which broke the token resets (#58; 1.3.4 added a fallback).
  Setting the state in the request coroutine fixes that by construction and restores an
  outer context instead of clearing it. The gain is correctness, not throughput.
  *Constraint:* Sync WSGI deployments work exactly as before. `USE_LOCAL_SET=True` stayed
  WSGI-only here until #96, which made it work on this async path without a request
  transaction (the GUC affinity guard applies the context to each statement).

### Tier 4: Async-safe decorator and testing helpers

- [x] **Async-aware `@with_rls_context`** — An `async def` function gets an
  `async def` wrapper that enters `async_tenant_context()` /
  `async_admin_context()` around the awaited body (#39). Generator functions
  are rejected at decoration time, and a sync-wrapped function that returns a
  coroutine raises `TypeError`.
  *Why:* The sync wrapper left the context before the coroutine ran, so the
  body ran under the caller's context: inside an outer admin context, a
  coroutine decorated for a tenant user saw every tenant.

- [x] **Async testing helpers** — `async_rls_bypass()`, `async_rls_as_tenant()`,
  `aassert_rls_enabled()`, `aassert_rls_policy_exists()` and
  `aassert_rls_blocks_without_context()` for `async def` tests (#40).
  *Why:* The sync helpers cannot run in an `async def` test (their database
  calls raise `SynchronousOnlyOperation`), and wrapping a `@contextmanager`
  in `sync_to_async` breaks the generator protocol. First-class async
  helpers remove that boilerplate and ensure correct usage.

- [x] **`assert_rls_blocks_without_context()` under `STRICT_MODE`** (#126) — The
  helper (and its async counterpart) counts the rows with raw SQL and the GUCs
  cleared, so it no longer raises `NoTenantContextError` under strict mode and
  works inside `rls_bypass()` / `rls_as_tenant()`.
  *Why:* The isolation check every test suite should have failed in exactly
  the configuration that asks for the strictest checking.

### Tier 5: Connection-affinity safety net

- [x] **ASGI error pages no longer leave GUCs on a worker thread's connection** (#130) —
  Django builds some ASGI error responses on a worker thread. A connection opened there
  during an authenticated request got the request's GUCs and kept them for later
  requests on that thread, anonymous ones included. The `connection_created` handler now
  applies the request's GUCs only on the thread that set them and clears them elsewhere.
  *Why:* A 404 page whose template or context processors query tenant data could
  show another tenant's rows, to anonymous visitors too.

- [x] **Clear RLS GUCs on every checkout from Django's connection pool** (#96) — The
  `connection_created` handler clears every GUC the policies read when a pooled
  connection is checked out, with or without a request context, and fails closed if it
  cannot. `django_rls_tenants.pool.reset_rls_gucs` clears them again as the connection
  goes back, and system check `W010` asks for it.
  *Why:* Django's pool keeps session GUCs between borrowers, and `W004` never fires for
  a pool (Django rejects a non-zero `CONN_MAX_AGE` with one).

- [x] **GUC-to-connection affinity guard** (#41) — The `connection_created`
  handler installs an execute wrapper on every connection that applies the
  active context's GUCs before a statement whenever the connection holds other
  values: a `sync_to_async` hop inherits a live context, sibling tasks keep
  their own, and a connection loses a context before the next statement that
  runs without it. See
  [GUC Affinity Guard](../advanced/architecture.md#guc-affinity-guard).
  *Why:* `django.db.connections` is per thread. Work on another thread
  (`thread_sensitive=False` hops, `asyncio.to_thread()`, the error pages Django
  builds on a worker thread) ran on a connection without the context's GUCs, or
  with an earlier request's. Sibling tasks of one request share a connection, so
  one task's context could change the GUCs between another task's statements.

- [x] **Transaction-local mode without a transaction** (#96) — With
  `USE_LOCAL_SET=True` the guard writes the context's GUCs transaction-locally: once per
  transaction, or around each autocommit statement in a transaction of its own. The
  middleware's async path, the context managers and `for_user()` no longer need
  `transaction.atomic()` on a configured alias, and a request on the async path works in
  this mode instead of raising `RLSConfigurationError`.
  *Why:* PgBouncer in transaction mode needs transaction-local GUCs, and Django has no
  async transactions, so requests on the middleware's async path could not use the mode
  at all.

- [x] **Documentation: async deployment guide** (#42) — The
  [Async Deployment](../guides/async.md) guide covers ASGI server
  configuration (Uvicorn, Daphne, Hypercorn), connection pooling
  considerations with async (`CONN_MAX_AGE`, Django's pool, PgBouncer) with a
  table of safe settings combinations, `ContextVar` propagation semantics
  across `sync_to_async` / `async_to_sync` boundaries, and the interaction
  between `SET LOCAL` (transaction-scoped) and Django's lack of async
  transactions.
  *Why:* Async deployment has different failure modes than WSGI. Users need
  guidance on connection lifecycle, GUC scoping, and which settings
  combinations are safe.

---

## v1.5.0 — DRF Integration & Query Improvements

First-class support for the most popular Django API framework.

- [ ] **Django REST Framework integration** (`contrib/drf.py`) —
  `RLSTenantAPIViewMixin` (opens the context after DRF authenticates, for any
  `APIView`, generic view or viewset), `RLSTenantPermission`,
  `RLSTenantSerializerMixin` / `RLSTenantModelSerializer` with
  `CurrentTenantDefault` (an auto-hidden tenant FK serializer field). The
  planned `TenantContextAuthentication` was dropped: an authentication class is
  never called back when the request ends, so it could not exit its context.
  Installable as `pip install django-rls-tenants[drf]`.
  *Why:* DRF is the dominant API framework for Django. Library-level
  integration removes boilerplate and prevents misconfiguration.

- [ ] **`prefetch_related` tenant filter propagation** — Extend ORM-level
  tenant filtering to prefetched querysets, matching the existing
  `select_related` behavior.
  *Why:* Completes defense-in-depth at the ORM layer. RLS still protects at
  the database level, but ORM filters enable composite index usage and make
  the isolation visible in query logs.

- [ ] **Granular strict mode** — `RLS_TENANTS["STRICT_MODE_EXCLUDE"]`, a list
  of `"app_label.ModelName"` labels that strict mode does not guard (a proxy
  follows its concrete model), with system check `W015` for an entry that names
  no installed RLS-protected model. Only the strict-mode check is skipped: the
  RLS policies, the auto-scope filter and `for_user()` are unchanged, so a
  listed model with no context still fails closed at the database. The main
  case is a user model read under `bypass_flag()` in an authentication backend,
  which sets a GUC but establishes no context. Models without an
  `RLSConstraint` (shared lookup tables) are never guarded and need no entry.
  *Why:* Current strict mode is all-or-nothing, which forces workarounds for
  legitimately unscoped models.

- [ ] **Migration helpers** — `check_rls --generate` flag that outputs a
  migration file for any missing RLS policies.
  *Why:* Reduces manual steps when adding new `RLSProtectedModel` subclasses
  to an existing project.

---

## v1.6.0 — Multi-Tenant Users & Observability

Common SaaS patterns and compliance tooling.

- [ ] **Multi-tenant user support** — Support for users belonging to multiple
  tenants with a tenant-switching helper and session/header-based tenant
  selection.
  *Why:* Many SaaS applications need users that operate across tenants
  (consultants, support staff, partner accounts). The current `TenantUser`
  protocol assumes a single `rls_tenant_id`.

- [ ] **Audit logging** — Structured logging when tenant context is set, cleared,
  or violated. Optional integration with Django's logging framework.
  *Why:* Compliance requirements (SOC 2, GDPR) often mandate audit trails for
  data access boundaries.

- [ ] **Connection pooling guide expansion** — Concrete PgBouncer and pgpool
  configuration examples, GUC leak troubleshooting section.
  *Why:* Connection pooling with session-scoped GUCs is the most common
  operational footgun — better docs prevent production incidents.

---

## v2.0.0 — Advanced Features

Architectural changes that may require migration effort.

- [ ] **Soft-delete awareness** — Optional `AND (deleted_at IS NULL)` clause in
  RLS policy expressions so soft-deleted rows are invisible even to the owning
  tenant.
  *Why:* Soft-delete is a common pattern; pushing it to RLS ensures deleted
  data cannot be accessed through any code path.

- [ ] **Cross-tenant relationships** — `RLSSharedModel` or `shared=True` flag
  for models that reference data across tenant boundaries (shared catalogs,
  inter-tenant messaging).
  *Why:* Real-world multi-tenant systems often need shared reference data that
  does not belong to a single tenant.

- [ ] **Row-level permissions beyond tenant/admin** — Additional GUC-based roles
  (viewer, editor, owner) with custom policy expressions.
  *Why:* The current binary admin/non-admin split is insufficient for
  applications with fine-grained access control needs.

- [ ] **Per-query strict mode override** —
  `Model.objects.rls_skip_strict().all()` for one-off queries that
  intentionally operate without tenant context.
  *Why:* Complements the per-model exclude list (v1.5.0) with a per-query
  escape hatch for ad-hoc operations.

- [ ] **Explicit through model helpers** — Templates and mixins for creating
  manually defined M2M through tables that are RLS-protected.
  *Why:* `register_m2m_rls()` only handles auto-generated through tables;
  explicit through models require manual constraint setup today.

---

## Future / Under Consideration

No committed timeline. Community interest drives prioritization.

- [ ] GraphQL integration helpers (Strawberry, Graphene)
- [ ] Django Ninja integration
- [ ] Benchmarking suite (100k+ rows, 1000+ tenants)
- [ ] Index-friendly policy shape -- the `CASE WHEN <admin> THEN true ELSE tenant_id = ...`
  policy can only be a post-scan filter; a bare `tenant_id = current_setting(...)::int`
  predicate is indexable (see the [Performance & Indexing guide](../guides/performance.md)).
  A shape that keeps the admin bypass yet stays indexable would only help policy-only
  scans (raw SQL without its own predicate), would need every deployment to recreate its
  policies, and risks mis-estimated admin scans. Not planned unless a concrete case appears.
- [ ] PostgreSQL 17+ RLS feature adoption
- [ ] Citus distributed table compatibility
