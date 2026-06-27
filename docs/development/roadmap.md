# Roadmap

> Last updated: 2026-06-27
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

- [ ] **New system checks** — `W008`: verify `TENANT_MODEL` resolves to an
  installed model at startup; `W009`: verify `TENANT_FK_FIELD` exists on all
  `RLSProtectedModel` subclasses.
  *Why:* These misconfigurations currently fail at query time with cryptic
  errors instead of being caught at startup.

- [ ] **Documentation: Celery quick-start** — Minimal pattern for wrapping Celery
  tasks with `tenant_context()` before full Celery integration lands in v1.3.0.
  *Why:* Celery is the most common question from new users — a docs page
  unblocks them immediately without waiting for library-level support.

---

## v1.3.0 — Django Admin & Developer Experience

Highest adoption impact, no new dependencies.

- [ ] **Tenant-aware Django Admin** — `RLSTenantModelAdmin` mixin that auto-sets
  tenant context from the admin user, hides the tenant FK field on forms,
  scopes changelists to the current tenant, and adds a tenant filter dropdown
  for admin users.
  *Why:* Every Django project uses the admin — tenant-unaware admin is a daily
  friction point.

- [ ] **Celery task integration** — `@rls_task` decorator and `RLSTask` base class
  that serialize tenant context into Celery task headers and restore it on the
  worker side. Supports task chains and groups.
  *Why:* Background tasks without tenant context are a data-leak risk. Users
  currently must manually wrap every task body in `tenant_context()`.

- [ ] **Raw SQL helpers** — `safe_tenant_sql()` utility for injecting
  `current_setting()` calls into raw SQL strings safely.
  *Why:* Raw SQL is sometimes unavoidable (reports, bulk operations), and
  there is no safe, documented pattern for it today.

- [ ] **Better error messages** — Add solution suggestions to
  `NoTenantContextError` (e.g., "did you forget to wrap this in
  `tenant_context()`?") and make system check hints more actionable.
  *Why:* Reduces time-to-fix for new users hitting common misconfiguration.

- [ ] **CODE_OF_CONDUCT.md** — Adopt the Contributor Covenant.
  *Why:* Standard open-source practice that signals project maturity and
  welcoming community.

---

## v1.4.0 — Async Support

First-class support for async Django (ASGI, async views, async ORM).

The current codebase uses `contextvars.ContextVar` for tenant state, which is
the correct primitive for async. However, every public API that touches the
database (GUC operations, context managers, middleware, decorator) is
synchronous. Under ASGI, Django wraps sync middleware in `sync_to_async`,
adding a thread-hop per request and creating connection-affinity issues where
GUCs are set on a connection in one thread but queries execute on a different
connection in another thread.

This release addresses the full async stack, from low-level GUC operations
up through middleware and context managers.

### Tier 1: Async GUC primitives (`rls/guc.py`)

- [ ] **`aset_guc()`, `aget_guc()`, `aclear_guc()`** — Async counterparts to
  the existing sync functions, using Django's async-compatible database API
  (`connection.cursor()` wrapped in `database_sync_to_async` or direct async
  cursor when Django supports it).
  *Why:* Every higher-level async API depends on non-blocking GUC operations.
  Without these, all async code paths force a thread-hop for every GUC call,
  negating the performance benefits of async.

### Tier 2: Async context managers (`tenants/context.py`, `rls/context.py`)

- [ ] **`async_tenant_context()`** — `@asynccontextmanager` variant of
  `tenant_context()` that uses `aset_guc()`/`aclear_guc()` internally.
  *Why:* `tenant_context()` uses `@contextmanager` (sync generator) and
  cannot be used with `async with`. Wrapping it in `sync_to_async` breaks
  the generator protocol.

- [ ] **`async_admin_context()`** — `@asynccontextmanager` variant of
  `admin_context()`.
  *Why:* Same issue as `tenant_context()` — sync generators are not
  async-compatible.

- [ ] **`async_rls_context()` and `async_bypass_flag()`** — Async variants
  of the generic `rls_context()` and `bypass_flag()` context managers.
  *Why:* Completes the async context manager surface area so users never
  need to manually bridge sync/async for RLS operations.

### Tier 3: Async middleware (`tenants/middleware.py`)

- [ ] **Async-native `RLSTenantMiddleware`** — Add `__acall__` (or the
  Django 4.1+ async middleware pattern) so the middleware runs natively in
  the ASGI event loop without `sync_to_async` thread-hops. Uses
  `aset_guc()`/`aclear_guc()` for GUC operations.
  *Why:* The current `MiddlewareMixin`-based middleware works under ASGI
  but forces a sync-to-async thread-hop per request. For high-concurrency
  ASGI deployments, this adds latency and limits throughput. A native async
  path eliminates the thread-pool bottleneck.
  *Constraint:* Must remain backward-compatible — sync WSGI deployments
  must continue working identically.

### Tier 4: Async-safe decorator and testing helpers

- [ ] **Async-aware `@with_rls_context`** — Detect whether the decorated
  function is a coroutine and return an async wrapper that uses
  `async_tenant_context()` / `async_admin_context()` instead of the sync
  variants.
  *Why:* The current decorator wraps the return value in a sync context
  manager. Decorating an `async def` view silently returns the coroutine
  object without awaiting it — a data-leak risk since no RLS context is
  established.

- [ ] **Async testing helpers** — `async_rls_bypass()`, `async_rls_as_tenant()`,
  and async variants of assertion helpers for use in async test functions.
  *Why:* Async test suites (e.g., `pytest-asyncio`) cannot use sync context
  managers without `sync_to_async` wrappers. First-class async helpers
  remove boilerplate and ensure correct usage.

### Tier 5: Connection-affinity safety net

- [ ] **GUC-to-connection affinity guard** — Ensure that when GUCs are set in
  one thread/context, queries that execute on a different connection (due to
  `sync_to_async` thread-hops) either inherit the GUC state or raise a clear
  error. Extend the existing `connection_created` signal handler to cover
  async connection acquisition patterns.
  *Why:* In async Django, `django.db.connections` is thread-local. A
  `sync_to_async` hop lands on a new thread with a fresh connection that
  does not have the GUC set. The existing `connection_created` signal
  handler partially covers this for lazily-created connections, but does
  not cover reused connections from a different request's thread.

- [ ] **Documentation: async deployment guide** — Covers ASGI server
  configuration (Uvicorn, Daphne, Hypercorn), connection pooling
  considerations with async (`CONN_MAX_AGE`, pgBouncer), `ContextVar`
  propagation semantics across `sync_to_async` / `async_to_sync` boundaries,
  and the interaction between `SET LOCAL` (transaction-scoped) and Django's
  async transaction handling.
  *Why:* Async deployment has different failure modes than WSGI. Users need
  guidance on connection lifecycle, GUC scoping, and which settings
  combinations are safe.

---

## v1.5.0 — DRF Integration & Query Improvements

First-class support for the most popular Django API framework.

- [ ] **Django REST Framework integration** (`contrib/drf.py`) —
  `RLSTenantPermission`, `RLSTenantViewSetMixin`,
  `TenantContextAuthentication`, and an auto-hidden tenant FK serializer field.
  Installable as `pip install django-rls-tenants[drf]`.
  *Why:* DRF is the dominant API framework for Django. Library-level
  integration removes boilerplate and prevents misconfiguration.

- [ ] **`prefetch_related` tenant filter propagation** — Extend ORM-level
  tenant filtering to prefetched querysets, matching the existing
  `select_related` behavior.
  *Why:* Completes defense-in-depth at the ORM layer. RLS still protects at
  the database level, but ORM filters enable composite index usage and make
  the isolation visible in query logs.

- [ ] **Granular strict mode** — `STRICT_MODE_EXCLUDE` setting to exempt
  specific models (e.g., shared lookup tables) from strict mode checks.
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
- [ ] PostgreSQL 17+ RLS feature adoption
- [ ] Citus distributed table compatibility
