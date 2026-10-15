# Connection Pooling

When using a connection pooler like **PgBouncer** or **pgpool-II**, or Django's own
connection pool, special care is needed to prevent GUC variables from leaking between
requests through pooled connections. Django's pool has its own section,
[Django's Connection Pool](#djangos-connection-pool). For which combinations are safe under
ASGI, see [Async Deployment](async.md#connections-and-pooling).

## The Problem

By default, django-rls-tenants uses `set_config()` to set session-scoped GUC variables.
These persist for the lifetime of the PostgreSQL session. With a connection pooler
in **transaction mode**, multiple Django requests may share the same PostgreSQL session,
and GUC values from one request could leak to the next.

## The Solution: USE_LOCAL_SET

Enable `USE_LOCAL_SET` to use `SET LOCAL` instead of `set_config()`:

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "myproject",
        "HOST": "pgbouncer-host",
        "PORT": "6432",
        "OPTIONS": {
            # PgBouncer connection
        },
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

### How SET LOCAL Differs

| Behavior | `set_config()` (default) | `SET LOCAL` |
|----------|------------------------|-------------|
| **Scope** | Session (connection) | Transaction |
| **Cleared when** | Explicitly by middleware | Automatically at `COMMIT`/`ROLLBACK` |
| **Connection pooling safe** | Only with cleanup | Yes |
| **Transaction per request** | No (autocommit around the view) | Synchronous path (WSGI): yes, opened by the middleware. Async path (ASGI): no (see below) |
| **Outside a transaction** | Written once, kept on the session | Each statement run under a context gets a transaction of its own (*1.4.0+*) |
| **Nesting support** | Full save/restore | Full save/restore with `SET LOCAL` |

Context managers restore the previous values when each block exits in both modes.
With `SET LOCAL`, transaction end also discards the transaction-local values. See
[Context Managers](context-managers.md#exceptions-inside-a-context-block) for how
restoration works after a failed statement or savepoint rollback.

### Where the Transaction Comes From

`SET LOCAL` only works inside a transaction. What supplies it depends on how the code
runs.

**WSGI requests.** Django's `ATOMIC_REQUESTS` is **not** the answer: it wraps only the view
callable, and the middleware sets the GUCs before the view runs. So when
`USE_LOCAL_SET=True`, `RLSTenantMiddleware` opens `transaction.atomic()` itself, on every
alias in `RLS_TENANTS["DATABASES"]`, around the rest of the middleware chain and the view.
The GUCs are in force for the whole request and disappear at the commit or rollback that
ends it. Unauthenticated requests set no GUCs and get no transaction.

`ATOMIC_REQUESTS=True` remains optional. If you enable it as well, Django's per-view
transaction simply nests as a savepoint inside the middleware's.

**The middleware's async path** (ASGI, when every middleware listed after it can run
asynchronously; *changed in 1.4.0*). It cannot hold a synchronous `transaction.atomic()`
open across the awaited chain, so it opens no transaction and writes no GUC. The
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) gives each statement
the request's GUCs instead (see
[Code Outside a Request Transaction](#code-outside-a-request-transaction)), or once per
transaction for a sync view under `ATOMIC_REQUESTS` (Django does not support
`ATOMIC_REQUESTS` for async views). **A WSGI request is atomic in this mode and a request
on the async path is not**: there, a view that writes twice can commit the first write and
fail on the second. Wrap the view's writes in `transaction.atomic()` where that matters.
If a synchronous-only middleware is listed after `RLSTenantMiddleware`, Django runs it
synchronously even under ASGI, and the request transaction applies as under WSGI. Before
1.4.0 the middleware raised `RLSConfigurationError` for this mode under ASGI.

!!! note "Consequences of a transaction per request (WSGI)"
    As with `ATOMIC_REQUESTS`, catch database
    errors inside your own `transaction.atomic()` block or the rest of the request fails
    with "current transaction is aborted"; `transaction.on_commit()` callbacks run when
    the request ends; and `@non_atomic_requests` has no effect, because the middleware's
    transaction encloses the view anyway. A streaming response iterates its body after
    the transaction has ended, so queries made while streaming run without tenant
    context (fail-closed: no rows) -- establish a context inside the generator.
    Place middleware implementing `process_exception` before `RLSTenantMiddleware`,
    so handled view exceptions cannot bypass rollback; unsafe ordering raises
    `RLSConfigurationError`. Error handling runs inside the transaction too, and writes
    by error handlers are rolled back along with the failed view's writes. On the async
    path there is no request transaction, so none of this applies (see
    [Middleware](middleware.md#use_local_set-and-asgi)).

### Code Outside a Request Transaction

*Changed in 1.4.0.* Management commands, Celery tasks, scripts and requests on the
middleware's async path have no transaction that the middleware opened. Before 1.4.0 a
context manager entered without `transaction.atomic()` raised `RuntimeError`, so you had
to open one yourself. Now the
context managers write nothing there: they set the Python-side context, and the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) writes the GUCs
transaction-locally (`set_config(name, value, true)`) on every alias in
`RLS_TENANTS["DATABASES"]`:

- **Inside a transaction** (`transaction.atomic()`, the WSGI request transaction,
  `AUTOCOMMIT=False`, a transaction you opened with SQL `BEGIN`), the guard sets both
  GUCs in one statement before the first statement that needs them, and again when the
  context changes in the same transaction (sibling asyncio tasks that share the
  connection, say). The values end with the transaction, committed or rolled back.
- **In autocommit**, a statement run while a context is active gets a transaction of its
  own: `BEGIN`, the `set_config()` statement, your statement, `COMMIT`. That is three
  extra round trips per statement. The statement keeps its autocommit semantics: it
  commits on its own, a failing one is rolled back alone, and a failing `COMMIT` (a
  deferred constraint) raises from that statement. An `executemany()` runs in one such
  transaction, so it commits or fails as a whole (in plain autocommit, psycopg2 keeps the
  rows before a failing one).
- **Outside any context**, a statement gets no transaction of the guard's. If the
  session's own values are unknown (see "After a context ends" below), the next such
  statement, in autocommit or in a transaction, first clears them for the session with one
  extra `set_config()` statement; inside a transaction the clear sticks once it commits.

```python
# No transaction.atomic() needed: each statement gets a transaction of its own.
with tenant_context(tenant_id=42):
    orders = list(Order.objects.all())
```

Where you enter the context decides what it costs:

| The context is entered | Cost |
|---|---|
| Outside any transaction | Nothing on entry or exit. Each autocommit statement run under it costs three extra round trips: `BEGIN`, a `set_config()` statement, `COMMIT`. |
| Outside any transaction, with a `transaction.atomic()` inside the block | One `set_config()` statement per transaction, before its first statement that needs the GUCs, plus one per context change in it. |
| Inside `transaction.atomic()` (a `TestCase`, the WSGI request transaction) | The block writes the GUCs itself: two `current_setting()` reads and two `SET LOCAL` statements on entry, two `SET LOCAL` statements on exit. The guard sends none for it while its tracker is clean (a test transaction, the WSGI request transaction, the first such block after a statement run outside any context). After an earlier context has ended on that connection it first clears the session's values, one more statement before the block's reads, and a loop that enters `atomic()` and a context on every iteration pays it every time. |

Putting a batch of statements in one transaction is still worth it for performance: either
placement beats three extra round trips per statement once the batch runs more than a few,
and the writes commit together.

```python
from django.db import transaction

# The context outside: one set_config() for the whole transaction.
with tenant_context(tenant_id=42):
    with transaction.atomic():
        orders = list(Order.objects.all())

# Or atomic() outside: the block writes its own GUCs, about six small statements
# (plus one clear first if an earlier context has ended on this connection).
with transaction.atomic():
    with tenant_context(tenant_id=42):
        orders = list(Order.objects.all())
```

What to know about the wrapped statements:

- The guard sends `BEGIN`, `COMMIT` and `ROLLBACK` past Django's cursor, so no execute
  wrapper sees them. It logs them the way Django logs its own transaction control, though:
  they appear in `connection.queries` (`assertNumQueries` and `CaptureQueriesContext`
  count them) and on the `django.db.backends` logger. A wrapped statement counts as
  **four** queries, listed in this order: `BEGIN`, the `set_config()` statement, `COMMIT`,
  then the statement itself, because Django logs it when it finishes.
- Statements PostgreSQL may refuse inside a transaction block run as they are, unwrapped
  and without the context: `VACUUM` (also `VACUUM (ANALYZE) t`), `CLUSTER`, `REINDEX`,
  `DISCARD`; `CREATE` / `ALTER` / `DROP` of a `DATABASE`, `TABLESPACE` or `SUBSCRIPTION`,
  and `ALTER SYSTEM`; `CREATE [UNIQUE] INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY`;
  and `ALTER TABLE ... DETACH PARTITION ... CONCURRENTLY`. The guard matches them on their
  grammar, from the statement's first words, so a statement that only mentions those words
  elsewhere (in a comment, say) is wrapped. None of them evaluates an RLS policy.
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` is wrapped. `SET TRANSACTION` is transaction
  control, in both GUC modes: the guard writes nothing before it, so an `atomic()` entered
  inside a context can start with it, and writes the context's values after it.
- A procedure or a `DO` block that commits or rolls back (`CALL` of a procedure with
  `COMMIT` inside, say) cannot run inside a context in autocommit: it fails with "invalid
  transaction termination". Run it outside any context.
- If the guard's `ROLLBACK` after a failed statement cannot be confirmed (a broken
  connection, a second failure, an interruption), the guard closes the connection, which
  Django believed to be in autocommit. The next statement reconnects, a pool rolls back a
  connection it gets back, and the statement's own error is the one that propagates.
- The guard covers only the aliases in `RLS_TENANTS["DATABASES"]`. A context manager
  entered outside `transaction.atomic()` on any other alias still raises, as before: add the
  alias to the list.
- It covers only the admin and tenant GUCs. Bypass flags set with `bypass_flag()`,
  `set_bypass_flag()` or `rls_context()` keep their explicit `is_local` parameter
  (default `False`).
- A statement run in autocommit inside a context gets the context's values in its own
  transaction, which override writes of the admin or tenant GUC that other code made for
  the session (`set_guc()`, `rls_context()` without `is_local=True`). Narrowing an
  `admin_context()` with `rls_context({"rls.is_admin": "false", ...})` has no effect there,
  though it does inside `transaction.atomic()` and in session mode. Change the context with
  `tenant_context()` / `admin_context()`, or write with `is_local=True` inside `atomic()`.
- SQL that bypasses Django's cursor (a driver cursor, `callproc()`, the `copy` methods, the
  `FETCH` of an open server-side cursor) is not seen. Outside a transaction it sees no
  context values in this mode, since nothing is left on the session.
- The guard is installed on connections that were already open when the app's `ready()`
  ran (opened in an earlier app's `ready()`, say), so a context applies on them too.

#### After a context ends

Code inside a context can write the admin or tenant GUC for the session (`set_guc()`,
`rls_context()` without `is_local=True`, a function, a `DO` block). In session mode the
context's exit restores the GUCs. In this mode nothing would, and the value would outlive
the context on a persistent or pooled connection. So when a context ends (a block exits, a
`for_user()` call's scope ends, a request served by `RLSTenantMiddleware` ends, the
`request_finished` safety net runs), the values that the sessions of the thread's
configured connections hold for themselves count as unknown. So do the session's values on
any connection after a statement that ran under a context's transaction-local values: that
reaches the connection of a `thread_sensitive=False` hop or a worker thread, which no block
exit on the request thread reaches. No statement runs when a session is marked. Before the
next statement run outside any context, in autocommit or in a transaction, the guard clears
the session's values with one extra `set_config()` statement: for example the next
request's session lookup on a persistent connection. That clear is for the session, so
once its transaction commits it sticks. Inside a transaction the mark itself takes effect
at the commit; a rollback undoes such a write anyway.

## PgBouncer Configuration

### Transaction Mode (recommended)

```ini title="pgbouncer.ini"
[databases]
myproject = host=localhost dbname=myproject

[pgbouncer]
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 20
```

With `pool_mode = transaction` and `USE_LOCAL_SET = True`:

- Each transaction gets a clean connection from the pool.
- `SET LOCAL` ensures GUCs are scoped to the transaction.
- Every statement gets its GUCs inside the transaction it runs in: the middleware's request
  transaction under WSGI, your `transaction.atomic()`, or, for requests on the middleware's
  async path and code outside a transaction, a transaction of its own that the GUC affinity
  guard opens (see
  [Code Outside a Request Transaction](#code-outside-a-request-transaction)). That is what
  transaction pooling needs. *Changed in 1.4.0:* before, only WSGI requests and code
  inside `atomic()` worked in this mode.
- No cleanup is needed after the transaction ends.
- Pass `is_local=True` to `bypass_flag()`, `set_bypass_flag()` and `rls_context()` (inside
  `transaction.atomic()`): they are session-scoped by default in either mode, and nothing
  clears a flag that PgBouncer hands to another client.
- Set `DISABLE_SERVER_SIDE_CURSORS = True` on the alias. Django requires it for transaction
  pooling: a server-side cursor (`iterator()`) can outlive its transaction, and the next
  transaction may run on another server connection.

### Session Mode

With `pool_mode = session`, each client gets a dedicated connection for the duration of
the session. The default `set_config()` behavior works correctly because the middleware
clears GUCs in `process_response`.

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": False,  # default, works with session mode
}
```

## Django's Connection Pool

Django 5.1+ can pool connections itself: set `DATABASES[alias]["OPTIONS"]["pool"]` with
psycopg 3 (`pip install "psycopg[pool]"`). Closing a pooled connection, which Django does
at the end of each request, hands it back to a `psycopg_pool.ConnectionPool`. The pool
rolls back an open transaction and does nothing else, so session GUCs set with
`set_config()` (`USE_LOCAL_SET=False`, the default) stay on the connection for its next
borrower.

### The Problem

The middleware clears the GUCs before the connection goes back. It cannot when its cleanup
failed or reached a different connection: a view that closed its connection before the
response ended (a long-lived SSE stream, for one), a crash, a cancelled request. The
`request_finished` safety net does not help: it runs after Django's own
`close_old_connections` has already returned the connection to the pool. The next
borrower, such as an anonymous request or a Celery task, would then run with the previous
request's tenant or admin bypass and could read that tenant's rows. A leaked
`extra_bypass_flags` entry (for example `rls.is_login_request`) would do the same for the
models that declare it, even for a borrower that sets its own tenant context. Versions
before 1.4.0 did not clear a pooled connection on checkout (see
[Upgrading](../advanced/migration-guide.md#from-134-to-140)).

`W004` never fires for a working pool, because Django rejects a non-zero `CONN_MAX_AGE`
together with `OPTIONS["pool"]`.

### What the Library Does on Checkout

Django sends `connection_created` on every checkout from the pool. For an alias in
`RLS_TENANTS["DATABASES"]` whose `OPTIONS["pool"]` is enabled (`True` or a non-empty dict;
Django treats `{}`, `False` and `None` as no pool), the `connection_created` handler clears
every GUC the library's RLS policies read, before the new borrower's first query:

- It is one statement, so one round trip per checkout (plus a `COMMIT` under
  `AUTOCOMMIT=False`), about one per request on that alias.
- It runs with or without a request context, in both GUC modes, and even when the pool has
  a `reset` callback (see below).
- Then, with `USE_LOCAL_SET=False`, on the thread that set the request's GUCs, the handler
  applies the request's GUCs as before (a connection checked out mid-request). A pooled
  connection always starts clean. The
  [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) then applies the
  active context before each statement, on whichever thread runs with one (with
  `USE_LOCAL_SET=True`, transaction-locally), and tracks each checkout as a new session.
- If the clear fails (a statement timeout, a broken connection), the checkout fails
  closed: the handler logs a warning (`Failed to clear RLS GUCs on a connection for alias
  ... checked out of a connection pool; discarding it.`), closes the connection so the pool
  discards it instead of reusing it, and raises the error to the code that asked for the
  connection. The borrower's query fails; it never runs on a connection that may still be
  dirty.

"Every GUC the policies read" means the configured tenant and admin GUCs (derived from
`GUC_PREFIX`) and, from the constraints of every installed model, each `RLSConstraint`'s
`guc_tenant_var`, `guc_admin_var` and `extra_bypass_flags` entries, and each
`RLSM2MConstraint`'s `guc_tenant_var` and `guc_admin_var`. GUCs read only by hand-written
policies, or by `AddM2MRLSPolicy` operations with non-default names, are not cleared.
Names that are not custom GUC names, such as an `extra_bypass_flags` entry without a dot
(PostgreSQL only accepts dotted names for custom settings), are left out, so they cannot
fail the statement.

Only Django's own pool (`OPTIONS["pool"]`) is detected, and only on aliases listed in
`RLS_TENANTS["DATABASES"]`. List every alias you use with `tenant_context(using=...)`,
`admin_context(using=...)` or `bypass_flag(using=...)` too. A third-party pooling backend
that hands out used connections is not detected.

### Clearing on Return: `reset_rls_gucs`

The checkout clear protects the next borrower, but an idle connection still holds the last
borrower's values until then. A pool `reset` callback clears them as the connection goes
back. Django forwards every key of `OPTIONS["pool"]` to `psycopg_pool.ConnectionPool`:

```python title="settings.py"
from django_rls_tenants.pool import reset_rls_gucs

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # ...
        "OPTIONS": {"pool": {"reset": reset_rls_gucs}},
    },
}
```

`psycopg_pool` calls it on the raw psycopg 3 connection, in one of its worker threads,
after rolling back any open transaction. `reset_rls_gucs` clears the same set of GUCs in one
statement and commits if the connection is not in autocommit, leaving it idle as the pool
requires. If it raises, the pool discards the connection instead of reusing it
(fail-closed). It costs one statement per return. With a `reset` callback, `psycopg_pool`
returns connections asynchronously through its worker, and a returned connection is not
available to waiting borrowers until the reset has run.

`django_rls_tenants.pool` imports nothing from the tenants layer at module level, so it is
safe to import from `settings.py` before Django is set up.

!!! note "Why not `DISCARD ALL` or `RESET ALL`"
    Django applies some session settings, such as the time zone, only once per pooled
    connection, through the pool's `configure=` callback; `init_connection_state` skips
    them for pooled connections. `RESET ALL` (which `DISCARD ALL` includes) would reset
    `TimeZone` to the session's default (the server, database or role setting), and
    nothing would set Django's again. `reset_rls_gucs`
    clears only the RLS GUCs. If you keep session GUCs of your own, write a `reset` that
    calls `reset_rls_gucs(conn)`, clears yours, and leaves the connection idle (commit
    unless it is in autocommit).

### With `USE_LOCAL_SET=True`

The GUCs set by the middleware, the tenant context managers and the GUC affinity guard
are transaction-local values, which end with the transaction, so a pooled connection never
holds them (*changed in 1.4.0:* also under ASGI and outside `transaction.atomic()`; before,
this was WSGI only and the middleware raised `RLSConfigurationError` under ASGI). Bypass
flags are not: `bypass_flag()`, `set_bypass_flag()` and `rls_context()` default to
`is_local=False` in either mode, so a flag whose block did not restore it can still go
back to the pool. System check `W010` does not fire in this mode; the checkout clear
still runs and covers those flags, and the next statement run outside any context after a
context has ended clears what the session may hold (see
[After a context ends](#after-a-context-ends)).

## System Checks

django-rls-tenants provides system checks for two pooling misconfigurations:

### W004: CONN_MAX_AGE with session GUCs

```
?: (django_rls_tenants.W004) DATABASES['default'] has CONN_MAX_AGE=600 with
USE_LOCAL_SET=False. This risks GUC state leaking between requests on persistent
connections. Consider USE_LOCAL_SET=True, which scopes the GUCs to transactions, so they
never stay on a connection.
	HINT: Set RLS_TENANTS['USE_LOCAL_SET'] = True, or set
DATABASES['default']['CONN_MAX_AGE'] = 0.
```

The alias and the configured `CONN_MAX_AGE` value (here `600`) are interpolated into the
message. It fires once for every alias in `RLS_TENANTS["DATABASES"]` with a non-zero or
`None` `CONN_MAX_AGE`.

**Fix:** Either:

- Set `USE_LOCAL_SET = True`, or
- Set `CONN_MAX_AGE = 0` (Django default, closes connections after each request).

### W010: Django's connection pool without a reset callback

```
?: (django_rls_tenants.W010) DATABASES['default'] uses Django's connection pool with
USE_LOCAL_SET=False and no pool 'reset' callback. A connection that a request's cleanup
did not reach goes back to the pool still holding that request's RLS GUCs, until its next
checkout clears them.
	HINT: Set DATABASES['default']['OPTIONS']['pool']['reset'] to
django_rls_tenants.pool.reset_rls_gucs, or set RLS_TENANTS['USE_LOCAL_SET'] = True.
```

It fires once for every alias in `RLS_TENANTS["DATABASES"]` whose `OPTIONS["pool"]` is
enabled without a `reset` (the pool is `True`, or a dict without `"reset"` or with
`"reset": None`), when `USE_LOCAL_SET=False`. Any `reset`, including your own, silences it.
The checkout clear (see above) protects the next borrower either way; the check asks for
the return-side clear so idle connections hold no tenant state.

**Fix:** Either:

- Set the pool's `reset` to `django_rls_tenants.pool.reset_rls_gucs` (see
  [Clearing on Return](#clearing-on-return-reset_rls_gucs)), or
- Set `USE_LOCAL_SET = True`. Session-scoped bypass flags can still go back to the pool in
  that mode; `reset_rls_gucs` clears them too.

!!! info "Retired checks"
    `W003` (1.3.1) and `W007` (1.3.2) warned about `USE_LOCAL_SET=True` without
    `ATOMIC_REQUESTS`. Since the middleware opens its own transaction, that combination
    is valid and the checks no longer exist. Check IDs are never reused.

## Django's Persistent Connections (CONN_MAX_AGE)

Django's built-in `CONN_MAX_AGE` setting controls how long database connections are
reused. With `USE_LOCAL_SET=False` and `CONN_MAX_AGE > 0`:

- The middleware clears GUCs in `process_response`.
- But if the response phase is skipped (e.g., due to an unhandled exception), GUCs
  could leak to the next request on the same connection.
- The `request_finished` signal handler provides a safety net for this case.

For maximum safety with persistent connections, enable `USE_LOCAL_SET`:

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "CONN_MAX_AGE": 600,  # 10 minutes
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

Under ASGI, keep `CONN_MAX_AGE = 0`: each request runs on a thread of its own, so a
persistent connection is never reused. Use Django's pool or PgBouncer instead (see
[Async Deployment](async.md#connections-and-pooling)).
