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
| `rls/guc.py` | `set_guc()`, `get_guc()`, `clear_guc()`, `restore_guc()` and their `a`-prefixed async counterparts -- manage PostgreSQL GUC variables |
| `rls/constraints.py` | `RLSConstraint` -- Django `BaseConstraint` that generates `CREATE POLICY` SQL. `RLSM2MConstraint` -- subquery-based policies for M2M join tables |
| `rls/context.py` | `rls_context()`, `bypass_flag()` and their `async with` counterparts `async_rls_context()`, `async_bypass_flag()` -- generic context managers for setting/restoring GUC variables |

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
| `tenants/context.py` | `tenant_context()`, `admin_context()`, `async_tenant_context()`, `async_admin_context()`, `@with_rls_context` |
| `tenants/middleware.py` | `RLSTenantMiddleware` -- per-request RLS context, with a native async path for ASGI |
| `tenants/affinity.py` | The GUC affinity guard -- an execute wrapper that applies the active context's GUCs on whichever connection a statement runs on (see [GUC Affinity Guard](#guc-affinity-guard)) |
| `tenants/bypass.py` | `set_bypass_flag()`, `clear_bypass_flag()`, `bypass_flag()`, `async_bypass_flag()` |
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

### Optional integrations

`contrib/` holds integrations with third-party libraries, each behind a pip extra:
`contrib/celery.py` (`celery`) and `contrib/drf.py` (`drf`). Neither `rls/` nor
`tenants/` imports them, nor Celery or Django REST Framework themselves (enforced by
`tests/test_layering.py`), so the core library imports without the extras. See the
[Celery](../guides/celery-tasks.md) and
[Django REST Framework](../guides/drf.md) guides.

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
it. The guard acts only on connections of an alias in `RLS_TENANTS["DATABASES"]`, in both
GUC modes (with `USE_LOCAL_SET=True` since 1.4.0, see "Transaction-local mode" below).
Before each statement there, it compares:

1. the GUC values of the active context, held in a private `ContextVar` that only the
   library's own entry points set (the context managers, the middleware, `for_user()`), to
   exactly the values they write;
2. the values the library last left on that database session.

When they differ, it first runs `SELECT set_config('rls.is_admin', ..., false),
set_config('rls.current_tenant', ..., false)` on the same connection (`true` instead of
`false` with `USE_LOCAL_SET=True`), with cleared values
outside any context. The statement goes through Django's cursor, so query logs show it, and
the `django_rls_tenants` logger names each one at `DEBUG` level. If it fails, the error
propagates and the original statement does not run. A context therefore applies to every
alias in `RLS_TENANTS["DATABASES"]`, whichever alias it was entered on (that alias may
itself be outside the list).

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
GUCs instead of restoring the ended context's values. A block that exits in another
`contextvars.Context` than the one it entered in (a generator suspended inside it and closed
or garbage-collected elsewhere) finds another context current: it restores its snapshot
only on the database session it entered on (a `SET LOCAL` there would otherwise outlive
the block; an ended enclosing context's values are cleared instead, as above), and leaves
the values unknown, so the guard applies the current context before the next statement.
A block the view leaves open in a request (a generator suspended inside it) is ended
before Django builds the error response for an exception, by `process_exception` or, for a
500 response it did not see (a view that returns `None`), by a `got_request_exception`
receiver, and when the request ends. The request's own context is not ended there, so the
error page inherits it, unless a left-open block was the current context: ending it leaves
the error page with no context (fail-closed). That holds with `USE_LOCAL_SET=True` under
WSGI too: the guard takes the block's values off the connection before the response's
first statement, and only SQL that bypasses Django's cursor could see them until then. A
block that a middleware listed after `RLSTenantMiddleware` holds around the rest of the
chain cannot be told apart from one left open, so it ends too when a 500 response is built
inside it (fail-closed for the rest of that block). Not covered: a 4xx response Django
builds for an exception raised outside the view (`Http404` from a later middleware), which
sends no signal; and a response an exception middleware listed after
`RLSTenantMiddleware` returns, which stops its `process_exception` (list exception
middleware before it).

`asgiref` copies `ContextVar` values from one context into another where they differ: into
the coroutine `async_to_sync` runs, which therefore inherits the block it is called in,
and back from a `sync_to_async` call into its caller, also when the call is cancelled
while its function is still inside a block. A block's entry never replaces an entry the
caller already has, so a cancelled call cannot hand its still-running block to a caller
inside a context, nor to a request the middleware serves: a request always holds an entry,
its own context's, the one it is served in, or one of no context, and a coroutine its view
runs with `async_to_sync` inherits it. Only the middleware's `process_request`, which runs
in a hop of its own under `MiddlewareMixin`, hands its context back. A coroutine outside
both (one started with `asyncio.run()`, say) does get the block of a call it cancels,
until that block ends.

When a block exits, the guard also takes its context off this thread's
other configured aliases right away, if it applied it there; a connection that fails then
is left for its next statement.

**What it records.** The library's own GUC writes (context entry and exit, the middleware,
`for_user()`, the `connection_created` handler) run with the guard out of the way and record
the values they leave. A context's exit restores its entry snapshot, and the guard brought
the session to the enclosing context's values before that snapshot was read, so code that
stays on one connection and thread runs no extra statement. Writes of the same GUCs by other
code (`set_guc()`, `rls_context()`) are not recorded and stay in force until the context
changes, as before: `assert_rls_blocks_without_context()` relies on that. With
`USE_LOCAL_SET=True` there is one exception: a statement run in autocommit inside a context
gets the context's values in its own transaction, which override them (see Limits). A
connection that
was already open when the app's `ready()` ran (opened in an earlier app's `ready()`, say)
gets the guard there; one that opened before the handler was connected in some other way
gets it at the library's first session GUC write on it.

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

**Transaction-local mode.** *Added in 1.4.0 (#96).* With `USE_LOCAL_SET=True` the guard
writes the context's values with `set_config(name, value, true)`, both in one statement.
They end with their transaction, committed or rolled back, so neither the session nor a
pooled connection keeps them; the tracker treats them as gone when the transaction ends,
and the next transaction applies them again. Where the statement runs decides what the
guard does:

- **In a transaction** (`transaction.atomic()`, the WSGI middleware's request transaction,
  `AUTOCOMMIT=False`, a transaction opened with SQL `BEGIN`), it applies the values before
  the first statement that needs them, and again when the context changes in the same
  transaction (sibling asyncio tasks that share the connection, say). For a context
  entered before the transaction, that is one `set_config()` statement per transaction.
  A context entered inside the transaction writes its own values eagerly instead: the
  block's two `current_setting()` reads and two `SET LOCAL` statements on entry, two
  `SET LOCAL` statements on exit. The guard sends nothing for it while its tracker is
  clean: in a test transaction (`TestCase`, `django_db`), in the WSGI request transaction,
  and for the first such block after a statement run outside any context. After an
  earlier context has ended on that connection, the guard first clears the session's own
  values (see "After a context ends"), one more statement before the block's reads, and a
  loop that enters `atomic()` and a context on every iteration pays it every time, because
  each block's end marks the session again.
- **In autocommit**, a statement run while a context is active gets a transaction of its
  own: `BEGIN`; `set_config(..., true)` for both GUCs; the statement; `COMMIT`. That is
  three extra round trips per statement, and entering or leaving the block costs nothing.
  An `executemany()` runs in one such transaction, so it commits or fails as a whole.
- **Outside any context**, a statement gets no transaction of the guard's. If the
  session's own values are unknown (see "After a context ends") or are a context's (after
  switching `USE_LOCAL_SET`, say), the guard first clears them for the session with one
  `set_config()` statement, in autocommit or in a transaction, where the clear sticks once
  the transaction commits. Otherwise there is no extra round trip.

The wrapped statement keeps its autocommit semantics: it commits on its own, a failing
statement is rolled back alone, and a failing `COMMIT` (a deferred constraint) raises from
that statement. If the `set_config()` fails, the error propagates and the statement does
not run. If the guard's `ROLLBACK` after a failure cannot be confirmed (a broken
connection, a second failure, an interruption), the guard closes the connection, because
Django believes it is in autocommit: the next statement reconnects, a pool rolls back a
connection it gets back, and the statement's own error is the one that propagates.
The guard sends `BEGIN`, `COMMIT` and `ROLLBACK` past Django's cursor, so no execute wrapper
sees them, and logs them the way Django logs its own transaction control
(`debug_transaction`): in `connection.queries`, where `assertNumQueries` and
`CaptureQueriesContext` count them, and on the `django.db.backends` logger. A wrapped
statement therefore counts as four queries, and `connection.queries` lists them in this
order: `BEGIN`, the `set_config()` statement, `COMMIT`, then the statement itself, because
Django logs it when it finishes, after the guard's `COMMIT`. A server-side cursor the
statement declares (Django's are `WITH HOLD` in autocommit, `.iterator()`) is computed at
that `COMMIT` with the values still set, as PostgreSQL computes a `WITH HOLD` cursor at the
end of its implicit transaction in plain autocommit.

Statements PostgreSQL may refuse inside a transaction block run as they are, unwrapped and
without the context: `VACUUM` (also with an option list, `VACUUM (ANALYZE) t`), `CLUSTER`,
`REINDEX`, `DISCARD`; `CREATE`, `ALTER` or `DROP` of a `DATABASE`, `TABLESPACE` or
`SUBSCRIPTION`, and `ALTER SYSTEM`; `CREATE [UNIQUE] INDEX CONCURRENTLY` and `DROP INDEX
CONCURRENTLY`; and `ALTER TABLE ... DETACH PARTITION name CONCURRENTLY`. None of them
evaluates an RLS policy. The guard matches each on its real grammar, from the statement's
first words: a statement that merely mentions those words elsewhere (in a comment, say) is
wrapped, and so is a real `CONCURRENTLY` statement with a comment among its first words,
which PostgreSQL then refuses loudly. `REFRESH MATERIALIZED VIEW CONCURRENTLY` is wrapped,
since it may run in a transaction. `SET TRANSACTION` (also `SET TRANSACTION SNAPSHOT`) is
transaction control, in both GUC modes: it must be a transaction's first statement, so the
guard writes nothing before it, which lets an `atomic()` entered inside a context start
with it. The guard writes the context's values after it, before the next statement that
needs them. Known limit: a procedure or a `DO` block that commits or rolls back (`CALL` of
a procedure with `COMMIT` inside, say) cannot run inside a context in autocommit:
PostgreSQL refuses it inside the guard's transaction ("invalid transaction termination").
Run it outside any context in this mode.

**After a context ends.** With `USE_LOCAL_SET=True` the library never writes the admin or
tenant GUC for the session, but code inside a context can (`set_guc()`, `rls_context()`
without `is_local=True`, a function, a `DO` block). In session mode the context's exit
restores the GUCs; in this mode nothing did, so such a value would outlive the context on
a persistent or pooled connection, and later statements outside any context would see that
tenant's rows. So when a context ends (a block exits, a `for_user()` call's scope ends, a
request served by `RLSTenantMiddleware` ends, the `request_finished` safety net runs),
`suspect_sessions()` marks what the sessions of the thread's configured connections hold
for themselves as unknown. The guard marks a connection's session the same way after any
statement that ran under a context's transaction-local values, in its own transaction or
in a transaction that holds local values. That reaches the connection of a
`thread_sensitive=False` hop or a worker thread, which no block exit on the request thread
reaches. No statement runs when a session is marked. Before the next statement run outside
any context, in autocommit or in a transaction, the guard clears the session's own values
with one extra `set_config()` statement, for example on the next request's session lookup
on a persistent connection. That clear is for the session, so once its transaction commits
it sticks. Inside a transaction the mark itself takes effect at the commit; a rollback
undoes such a write anyway.

The library's own entry points cooperate. In this mode the context managers, the
`for_user()` scope and the middleware on its async path write nothing outside a
transaction (inside `transaction.atomic()` they still write `SET LOCAL` values on entry
and restore them on exit; a transaction still open at a block's exit, from
`AUTOCOMMIT=False` or an SQL `BEGIN`, may get one `set_config()` statement that puts the
enclosing context's values back, or clears them for the session when there is none), and
the `connection_created` handler no longer writes the
request's GUCs eagerly on a connection opened on the request thread. A `for_user()` call
that opens a transaction of its own (`update_or_create()`, `bulk_create()`,
`bulk_update()`, `delete()`) on a configured alias lets the guard set the values in it,
one `set_config()`, and take them off at the scope's exit, one more. At `DEBUG` level the
`django_rls_tenants` logger names every write the guard makes, the clear outside a context
included, except the `set_config()` inside its own `BEGIN` ... `COMMIT`; that one shows
only in `connection.queries` and on `django.db.backends`, with the `BEGIN` and `COMMIT`
around it.

**Limits.**

- Aliases in `RLS_TENANTS["DATABASES"]` only. An alias outside the list does not follow a
  context, and with `USE_LOCAL_SET=True` a context manager entered outside
  `transaction.atomic()` on such an alias still raises (the sync managers `RuntimeError`,
  the async ones `RLSConfigurationError`).
- One Django connection is taken to be one database session. Behind an external pooler in
  transaction mode (PgBouncer) it is not, and the guard cannot know which server connection
  a statement reaches with session GUCs: use `USE_LOCAL_SET=True` there. The guard then
  writes only transaction-local values, in the same transaction as the statement, and
  leaves nothing on any server connection.
- The admin and tenant GUCs only. Bypass flags and other GUCs set with `bypass_flag()` or
  `rls_context()` stay on the connection they were set on, and sibling tasks see them.
- With `USE_LOCAL_SET=True`, a statement run in autocommit inside a context gets the
  context's values in its own transaction, which override writes of the admin or tenant GUC
  that other code made for the session (`set_guc()`, `rls_context()` without
  `is_local=True`). Narrowing an `admin_context()` with
  `rls_context({"rls.is_admin": "false", ...})` therefore has no effect there, though it
  does inside `transaction.atomic()` and in session mode. Change the context with
  `tenant_context()` / `admin_context()`, or write with `is_local=True` inside `atomic()`.
- Only contexts the library set up. Code that builds a context by hand with
  `set_current_tenant_id()` / `set_rls_context_active()` and writes the GUCs itself is left
  alone: the guard never infers a context from those functions (an active flag without a
  tenant would otherwise mean admin).
- SQL that bypasses Django's execute wrappers is not seen: a driver cursor,
  `CursorWrapper.callproc()`, the driver's `copy` methods, and the `FETCH`es of a server-side
  cursor that is already open. On a connection the guard has used, such SQL sees what it last
  left there until the next statement it sees: a worker thread's connection can hold the
  context of the last hop it ran (with `USE_LOCAL_SET=True`, outside a transaction, such SQL
  sees no context values, since nothing is left on the session). Inside a transaction
  PostgreSQL computes a server-side cursor's rows as they are fetched (outside one, when it
  opens), so a sibling task's statement between two fetches changes the GUCs the later rows
  are checked against.
- A transaction that ends where the guard cannot see it and is followed by one it cannot see
  start (both on the driver connection) can leave it trusting values the rollback undid; the
  next statement through Django's cursor after a visible boundary corrects it.
- The cost is about a microsecond of Python per statement, and no extra round trip while
  the connection already holds the context's values. With `USE_LOCAL_SET=True` the Python
  cost rises by about 0.1 to 0.15 µs outside a transaction and by almost nothing inside
  one. The round trips depend on where the context is entered: three extra per statement
  run under it in autocommit; one `set_config()` per transaction (plus one per context
  change inside it) for a transaction opened inside it; and for a block entered inside
  `transaction.atomic()`, the block's own six small statements (two reads, two writes on
  entry, two on exit), with none from the guard while its tracker is clean and one clear
  before them when an earlier context has ended on that connection.

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
  (`order.items.all()`) later, after the call, under whatever context is active then; so
  do their `save()`, `delete()` and `refresh_from_db()`.

Use `tenant_context()` / `admin_context()` around such code.

Each scope costs two `current_setting()` reads, plus one write and one restore for every
GUC that differs from the user's value: six small statements for a tenant user outside
any context, four for an admin (whose tenant GUC is already empty), two inside a matching
context. With `USE_LOCAL_SET=True` inside `transaction.atomic()` the writes are
`SET LOCAL`. Outside `atomic()`, on an alias in `RLS_TENANTS["DATABASES"]`, a scope reads
and writes nothing (*changed in 1.4.0*): it sets the `ContextVar` values, and the guard
applies the user's GUCs to each statement, in a transaction of its own in autocommit (see
"Transaction-local mode" in [GUC Affinity Guard](#guc-affinity-guard)), so every method
works there instead of raising. `update_or_create()`, `bulk_create()`, `bulk_update()` and
`delete()`, which Django runs in a transaction anyway, still open one when none is open; on
a configured alias their own scope then does not read, set or restore anything inside it:
the guard sets the user's values in that transaction (one `set_config()`) and takes them
off at the scope's exit (one more). Scopes nested in that transaction (a `_fetch_all()` the
call runs, say) still run their two `current_setting()` reads each (tracked in #137). On an
alias outside the list, a method that has to change a
GUC still writes `SET LOCAL` and raises `RuntimeError` outside `transaction.atomic()`,
except those four methods, which open their own.

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
