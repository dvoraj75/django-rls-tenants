# Architecture

django-rls-tenants is organized into two internal layers with a strict import boundary.

## Two-Layer Design

```
┌─────────────────────────────────────────────┐
│              tenants/ layer                 │
│  (Django multitenancy: models, middleware,  │
│   context managers, managers, testing)      │
│                                             │
│  Imports from: rls/, django, stdlib         │
├─────────────────────────────────────────────┤
│                rls/ layer                   │
│  (Generic PostgreSQL RLS primitives: GUC    │
│   helpers, RLSConstraint, context managers) │
│                                             │
│  Imports from: django, stdlib ONLY          │
└─────────────────────────────────────────────┘
```

### rls/ Layer

The lower layer provides generic PostgreSQL RLS primitives with **zero knowledge of
tenants, users, or Django multitenancy concepts**:

| Module | Purpose |
|--------|---------|
| `rls/guc.py` | `set_guc()`, `get_guc()`, `clear_guc()` -- manage PostgreSQL GUC variables |
| `rls/constraints.py` | `RLSConstraint` -- Django `BaseConstraint` that generates `CREATE POLICY` SQL. `RLSM2MConstraint` -- subquery-based policies for M2M join tables |
| `rls/context.py` | `rls_context()` -- generic context manager for setting/restoring GUC variables |

This layer is reusable outside the multitenancy use case. You could use `RLSConstraint`
and `rls_context()` to implement any RLS-based access control pattern.

### tenants/ Layer

The upper layer builds Django multitenancy on top of `rls/`:

| Module | Purpose |
|--------|---------|
| `tenants/conf.py` | `RLSTenantsConfig` -- reads `RLS_TENANTS` settings |
| `tenants/models.py` | `RLSProtectedModel` -- abstract base with auto-FK and `RLSConstraint`. `register_m2m_rls()` -- auto-detection of M2M fields |
| `operations.py` | `AddM2MRLSPolicy` -- reversible migration operation for M2M RLS policies |
| `pool.py` | `reset_rls_gucs()` -- `reset` callback for Django's psycopg connection pool. Also the single statement that clears every GUC the policies read, which the `connection_created` handler runs on each checkout from a pool. Importable from `settings.py` (no `tenants/` import at module level) |
| `tenants/managers.py` | `TenantQuerySet`, `RLSManager` -- lazy GUC setting at query eval time |
| `tenants/context.py` | `tenant_context()`, `admin_context()`, `@with_rls_context` |
| `tenants/middleware.py` | `RLSTenantMiddleware` -- per-request RLS context, with a native async path for ASGI |
| `tenants/affinity.py` | The GUC affinity guard -- an execute wrapper that applies the active context's GUCs on whichever connection a statement runs on (see [GUC Affinity Guard](#guc-affinity-guard)) |
| `tenants/bypass.py` | `set_bypass_flag()`, `clear_bypass_flag()`, `bypass_flag()` |
| `tenants/testing.py` | Test helpers: `rls_bypass`, `rls_as_tenant`, assertion functions, and their async counterparts (`async_rls_bypass`, `async_rls_as_tenant`, `aassert_*`) |
| `tenants/types.py` | `TenantUser` protocol |
| `tenants/state.py` | `get_current_tenant_id()`, `set_current_tenant_id()`, `reset_current_tenant_id()` -- `ContextVar`-based tenant state for auto-scoping. Also `get_rls_context_active()`, `set_rls_context_active()`, `reset_rls_context_active()` -- tracks whether any RLS context is active (used by strict mode). A private third `ContextVar` holds the GUC values the library applied for the active context, for the GUC affinity guard |
| `tenants/checks.py` | Django system checks (W001--W010) |

### Import Boundary

The `rls/` layer **must never import from `tenants/`**. This boundary is enforced by
`tests/test_layering.py`, which scans all `rls/` source files for forbidden imports.

This constraint ensures:

- The `rls/` primitives remain generic and reusable.
- Changes to the `tenants/` layer cannot break the `rls/` layer.
- The dependency graph is a clean DAG (no cycles).

## GUC Variable Flow

PostgreSQL GUC (Grand Unified Configuration) variables are the mechanism for
communicating tenant identity from Django to RLS policies:

```
Django Application           PostgreSQL
─────────────────           ──────────

Middleware reads             set_config('rls.current_tenant', '42')
request.user        ──────▶  set_config('rls.is_admin', 'false')

ORM query executes  ──────▶  SELECT * FROM orders
                             WHERE ... (+ RLS policy filter)

                             RLS policy evaluates:
                             current_setting('rls.current_tenant') = '42'
                             tenant_id = 42  ✓ (row returned)
                             tenant_id = 99  ✗ (row hidden)

Middleware cleanup  ──────▶  set_config('rls.current_tenant', '')
                             set_config('rls.is_admin', '')
```

## GUC Affinity Guard

*Added in 1.4.0 (#41).* A context sets the admin and tenant GUCs when it starts, on the
connection that is current at that moment. Django's connections are per thread, so SQL can
still run on another connection while the context is live, or on the same connection after
something else has changed the GUCs:

- a `sync_to_async(..., thread_sensitive=False)` hop or `asyncio.to_thread()` runs on another
  thread, with a copy of the context's `ContextVar`s but with that thread's own connection,
  and so does an error page Django builds there under ASGI;
- sibling `asyncio` tasks of one request share its connection, so one task's context can
  change the GUCs between another task's statements;
- a context entered on one alias leaves the other aliases as they were.

The `connection_created` handler puts an execute wrapper, the guard, first in the
`execute_wrappers` of every PostgreSQL connection, and Django runs every statement through
it. The guard acts only on connections of an alias in `RLS_TENANTS["DATABASES"]`, with
`USE_LOCAL_SET=False`. Before each statement there, it compares:

1. the GUC values of the active context, held in a private `ContextVar` that only the
   library's own entry points set (the context managers, the middleware, `for_user()`), to
   exactly the values they write;
2. the values the library last left on that database session.

When they differ, it first runs `SELECT set_config('rls.is_admin', ..., false),
set_config('rls.current_tenant', ..., false)` on the same connection, with cleared values
outside any context. The statement goes through Django's cursor, so query logs show it, and
the `django_rls_tenants` logger names each one at `DEBUG` level. If it fails, the error
propagates and the original statement does not run. A context therefore applies to every
alias in `RLS_TENANTS["DATABASES"]`, whichever alias it was entered on, including one
outside the list.

```
Request thread                       Worker thread (hop with a copy of the context)
──────────────                       ──────────────────────────────────────────────
tenant_context(42)
  set_config(... '42') ──▶ conn A
  sync_to_async(f, thread_sensitive=False)
                                     f(): cursor.execute("SELECT ...")
                                       guard: context (false, 42), conn B holds nothing
                                       set_config(... '42') ──▶ conn B
                                       SELECT ...           ──▶ conn B
                                     later job without a context:
                                       guard: no context, conn B holds (false, 42)
                                       set_config(... '')   ──▶ conn B
                                       SELECT ...           ──▶ conn B
```

**Live contexts only.** A copy of the context (a hop, a task, `copy_context()`) shares the
context's entry in that `ContextVar` with the block that made it, and the block's exit ends
it for every copy. A hop or task awaited inside the block inherits the context; one that
runs after the block has ended gets nothing, as on a connection without a context. A block
such a copy entered itself, and that exits after the enclosing block has ended, clears the
GUCs instead of restoring the ended context's values. `asgiref` copies the `ContextVar`
values of a `sync_to_async` call back into its caller, also when the call is cancelled
while its function is still inside a block; a block's entry compares equal to any value,
so `asgiref` leaves the caller's own context in place (only the middleware's
`process_request`, which runs in a hop of its own under `MiddlewareMixin`, hands its
context back). When a block exits, the guard also takes its context off this thread's
other configured aliases right away, if it applied it there; a connection that fails then
is left for its next statement.

**What it records.** The library's own GUC writes (context entry and exit, the middleware,
`for_user()`, the `connection_created` handler) run with the guard out of the way and record
the values they leave. A context's exit restores its entry snapshot, and the guard brought
the session to the enclosing context's values before that snapshot was read, so code that
stays on one connection and thread runs no extra statement. Writes of the same GUCs by other
code (`set_guc()`, `rls_context()`) are not recorded and stay in force until the context
changes, as before: `assert_rls_blocks_without_context()` relies on that. A connection that
opened before the handler was connected (during another app's `ready()`, say) gets the guard
at the library's first GUC write on it.

**Rollbacks.** PostgreSQL undoes a `set_config()` made in a transaction or savepoint that is
rolled back. The guard follows:

- Django's commit and rollback of the connection (`atomic()`, `transaction.commit()` /
  `rollback()`): `install()` wraps the connection's `_commit()` and `_rollback()` and reads
  the transaction status before and after. A commit keeps the values; a rollback, or a
  commit of a failed transaction (which PostgreSQL turns into a rollback without an
  error), brings back those from before the transaction; a call that ended nothing
  (psycopg2 sends nothing in autocommit mode) changes nothing.
- the transaction-control statements that run through Django's cursor: a savepoint records
  the values at that point and `ROLLBACK TO` it puts them back; `ROLLBACK` and `COMMIT` sent
  as SQL are followed like Django's, and a `COMMIT` of a failed transaction, which
  PostgreSQL turns into a rollback, as one. `RESET ALL`, `DISCARD ALL` and `RESET` of the
  admin or tenant GUC make the values unknown. Leading comments are skipped; a string with
  several statements, a statement that is not text, or a savepoint name the guard cannot
  read makes them unknown.
- the transaction status the driver reports (libpq, without a round trip), for a transaction
  that ended where the guard could not see it (a commit on the driver connection): the
  values recorded during it are kept only if they equal those from before it, since commit
  and rollback then leave the same thing.

Unknown values are applied again before the next statement. The guard never runs its
statement before a transaction-control statement, in a failed transaction, or while Django
has marked the transaction for rollback: those statements succeed or fail on their own.

**Limits.**

- Session GUCs only. With `USE_LOCAL_SET=True` the guard does nothing and never writes a
  session GUC.
- One Django connection is taken to be one database session. Behind an external pooler in
  transaction mode (PgBouncer) it is not, and the guard cannot know which server connection
  a statement reaches: use `USE_LOCAL_SET=True` there, as before.
- The admin and tenant GUCs only. Bypass flags and other GUCs set with `bypass_flag()` or
  `rls_context()` stay on the connection they were set on, and sibling tasks see them.
- Only contexts the library set up. Code that builds a context by hand with
  `set_current_tenant_id()` / `set_rls_context_active()` and writes the GUCs itself is left
  alone: the guard never infers a context from those functions (an active flag without a
  tenant would otherwise mean admin).
- SQL that bypasses Django's execute wrappers is not seen: a driver cursor,
  `CursorWrapper.callproc()`, the driver's `copy` methods, and the `FETCH`es of a server-side
  cursor that is already open. On a connection the guard has used, such SQL sees what it last
  left there until the next statement it sees: a worker thread's connection can hold the
  context of the last hop it ran. Inside a transaction PostgreSQL computes a server-side
  cursor's rows as they are fetched (outside one, when it opens), so a sibling task's
  statement between two fetches changes the GUCs the later rows are checked against.
- A transaction that ends where the guard cannot see it and is followed by one it cannot see
  start (both on the driver connection) can leave it trusting values the rollback undid; the
  next statement through Django's cursor after a visible boundary corrects it.
- The cost is a few microseconds of Python per statement, and no extra round trip while the
  connection already holds the context's values.

## Policy Generation

The `RLSConstraint` generates SQL during Django migrations:

```sql
-- Generated by RLSConstraint
ALTER TABLE "myapp_order" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "myapp_order" FORCE ROW LEVEL SECURITY;

CREATE POLICY "myapp_order_tenant_isolation_policy"
ON "myapp_order"
USING (
    CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
         THEN true
         ELSE tenant_id = nullif(
             (SELECT current_setting('rls.current_tenant', true)), '')::int
    END
)
WITH CHECK (
    CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
         THEN true
         ELSE tenant_id = nullif(
             (SELECT current_setting('rls.current_tenant', true)), '')::int
    END
);
```

Key design decisions in the policy:

- **`CASE WHEN ... THEN true ELSE ...`**: the `CASE` structure clearly separates
  admin bypass from tenant matching. For admin queries the tenant check is skipped.
  For normal tenant queries the expression reduces to a simple `tenant_id = <value>`
  equality. Since v1.3.0 each ``current_setting()`` read is wrapped in an
  uncorrelated scalar sub-SELECT -- ``(SELECT current_setting('rls.current_tenant',
  true))`` -- which PostgreSQL evaluates once per statement (an InitPlan) instead
  of per row; the predicate is otherwise identical. The larger performance benefit
  still comes from auto-scoping (``WHERE tenant_id = X`` in ``get_queryset()``),
  which enables composite index usage. The ``CASE`` form is also why the policy is
  never an index condition: see
  [What the planner sees](../guides/performance.md#what-the-planner-sees).
- **`current_setting(..., true)`**: the `true` parameter returns empty string instead
  of raising an error when the GUC is not set. This enables fail-closed behavior.
- **`nullif(..., '')`**: converts empty string to `NULL`, making the
  `tenant_id = NULL` comparison false (no rows match -- fail-closed).
- **`::int` cast**: ensures type safety. The cast type is configurable via `TENANT_PK_TYPE`.
- **`FORCE ROW LEVEL SECURITY`**: ensures RLS applies even to the table owner.
  It does not affect superusers or `BYPASSRLS` roles, which always skip RLS;
  system check `W005` warns when the default connection uses either.

## M2M Policy Generation

For M2M through tables, `RLSM2MConstraint` and `AddM2MRLSPolicy` generate
`EXISTS`-based subquery policies instead of direct `tenant_id` checks, since
through tables have no tenant FK column:

```sql
-- Generated by RLSM2MConstraint / AddM2MRLSPolicy
CREATE POLICY "myapp_project_members_m2m_rls_policy"
ON "myapp_project_members"
USING (
    CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
         THEN true
         ELSE EXISTS (SELECT 1 FROM "myapp_project"
                      WHERE id = project_id
                      AND tenant_id = nullif(
                          (SELECT current_setting('rls.current_tenant', true)), '')::int)
              AND EXISTS (SELECT 1 FROM "myapp_user"
                          WHERE id = user_id
                          AND tenant_id = nullif(
                              (SELECT current_setting('rls.current_tenant', true)), '')::int)
    END
)
WITH CHECK (...same...);
```

`EXISTS` is preferred over `IN (SELECT ...)` because it gives the PostgreSQL
planner more optimisation flexibility (semi-join vs nested loop).

The auto-detection in `register_m2m_rls()` runs during `AppConfig.ready()` and
discovers all auto-generated through tables on `RLSProtectedModel` subclasses.
Explicit through models are skipped -- users manage those via standard
`RLSProtectedModel` inheritance.

## Lazy QuerySet Evaluation

`TenantQuerySet.for_user()` solves a timing problem: Django querysets are lazy, so
the GUCs must be set when the query actually executes, not when `for_user()` is called.

```python
# qs is created here, but no SQL runs yet
qs = Order.objects.for_user(user)

# Query might be evaluated much later (e.g., in a template)
for order in qs:  # <-- GUCs are set HERE, around the SELECT
    print(order.title)
```

`for_user()` only stores the user. Every `TenantQuerySet` method that runs SQL wraps its
statements in one private context manager, `_rls_scope()`, which:

1. resolves the alias the SQL runs on (`self.db`, or the router's write alias for the
   write methods) once, and pins it on the queryset for the call, so a router that
   answers differently each time cannot split the GUCs from the query;
2. reads the admin and tenant GUCs there, and sets those that differ from the user's
   values. Inside a context that has already applied them (the middleware, for the same
   user) it writes nothing;
3. sets the tenant state `ContextVar`s, so code that runs during the call (the
   `prefetch_related()` queries, `delete()` cascades, signal handlers, Celery tasks
   enqueued there) is auto-scoped to the user's tenant, passes `STRICT_MODE` and, like
   the SQL, runs as the user: as an admin, for an admin user;
4. on exit, resets the `ContextVar`s and restores the GUCs it changed to the values it
   read in step 2.

Restoring instead of clearing keeps an enclosing `tenant_context()`, `admin_context()`
or middleware context intact after a `for_user()` query, in both GUC modes, and lets
scopes nest. When the statement fails and aborts the transaction, the restore fails too:
that failure is logged and dropped, the rollback undoes the change, and the statement's
own error propagates (the same rule as the context managers).

| Methods | Where the scope is entered |
|---|---|
| Iteration, `len()`, `bool()`, indexing and slicing, `get()`, `first()`, `last()`, `earliest()`, `latest()`, `in_bulk()`, iterating the result of `values()` / `values_list()` | `_fetch_all()` |
| `count()`, `exists()` (and `contains()`, which calls it), `aggregate()`, `explain()` | the method itself |
| `iterator()`, `aiterator()` | once per chunk |
| `update()`, `delete()`, `create()` (and `get_or_create()`, which calls it), `bulk_create()`, `bulk_update()`, `update_or_create()` | the method itself, on the write alias |
| `acount()`, `aget()`, `acreate()`, `async for` and the other async methods | the sync method they run in a `sync_to_async` hop |

`iterator()` fetches each chunk (2000 rows by default), together with its
`prefetch_related()` lookups, inside its own scope, and closes the scope while your loop
body runs. A server-side cursor inside a transaction produces its rows at fetch time, and
a part of the plan that a later fetch reaches first (a `UNION ALL` branch, say) evaluates
the RLS policy only then, so every fetch needs the GUCs. A scope held for the whole loop
would also run the loop body as the user and leave the GUCs on the connection when the
loop stops early. `aiterator()` pulls each chunk in one thread-sensitive hop and enters
the scope inside that hop.

A queryset that has already been evaluated answers iteration, `len()`, `count()`,
`exists()` and an ordered `first()` from its result cache. That runs no SQL, so there is
no scope and no `STRICT_MODE` check. The querysets `prefetch_related()` caches on each
instance work the same way, so a template can read them after the scope has ended.

A `for_user()` queryset does not cover SQL that runs outside its own methods:

- `raw()` returns a `RawQuerySet`, which does not carry the user.
- Used as a subquery (`.filter(order__in=Order.objects.for_user(user))`), it is compiled
  into the outer query: its ORM filter applies, its GUCs do not.
- Combining querysets with `|`, `&`, `^` or `union()` keeps only the left operand's user,
  and a sliced operand turns the result into a plain queryset.
- Instances it returned load deferred fields, foreign keys and related managers
  (`order.items.all()`) later, after the call, under whatever context is active then.

Use `tenant_context()` / `admin_context()` around such code.

Each scope costs two `current_setting()` reads, plus one write and one restore for every
GUC that differs from the user's value: six small statements for a tenant user outside
any context, four for an admin (whose tenant GUC is already empty), two inside a matching
context. With `USE_LOCAL_SET=True` the writes are `SET LOCAL`, which needs a
transaction. `update_or_create()`, `bulk_create()`, `bulk_update()` and `delete()`,
which Django runs in a transaction anyway, open one when none is open; any other method
that has to change a GUC raises `RuntimeError` outside `transaction.atomic()`, as
iteration always did.

Before 1.4.0 only `_fetch_all()` applied the user's GUCs: `count()`, `exists()`,
`aggregate()`, `iterator()`, the writes and their async variants ran without them, and
found no rows under a role that RLS applies to (#131).

## Defense-in-Depth

For tenant (non-admin) users, `for_user()` applies **two layers** of filtering:

1. **Django ORM filter**: `qs.filter(tenant_id=user.rls_tenant_id)` -- catches
   misconfigurations at the application level.
2. **RLS policy**: `current_setting('rls.current_tenant')` comparison -- enforces
   isolation at the database level, even for raw SQL.

Neither layer alone is sufficient:

- The ORM filter can be bypassed with raw SQL.
- The RLS policy could be misconfigured.

Together, they provide defense-in-depth.
