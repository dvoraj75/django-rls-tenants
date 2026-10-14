# Connection Pooling

When using a connection pooler like **PgBouncer** or **pgpool-II**, or Django's own
connection pool, special care is needed to prevent GUC variables from leaking between
requests through pooled connections. Django's pool has its own section,
[Django's Connection Pool](#djangos-connection-pool).

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
| **Transaction per request** | No (autocommit around the view) | Yes, opened by the middleware |
| **Nesting support** | Full save/restore | Full save/restore with `SET LOCAL` |

Context managers restore the previous values when each block exits in both modes.
With `SET LOCAL`, transaction end also discards the transaction-local values. See
[Context Managers](context-managers.md#exceptions-inside-a-context-block) for how
restoration works after a failed statement or savepoint rollback.

### Where the Transaction Comes From

`SET LOCAL` only works inside a transaction. Django's `ATOMIC_REQUESTS` is **not** the
answer: it wraps only the view callable, and the middleware sets the GUCs before the view
runs. So when `USE_LOCAL_SET=True`, `RLSTenantMiddleware` opens `transaction.atomic()`
itself, on every alias in `RLS_TENANTS["DATABASES"]`, around the rest of the middleware
chain and the view. The GUCs are in force for the whole request and disappear at the
commit or rollback that ends it. Unauthenticated requests set no GUCs and get no
transaction.

`ATOMIC_REQUESTS=True` remains optional. If you enable it as well, Django's per-view
transaction simply nests as a savepoint inside the middleware's.

!!! note "Consequences of a transaction per request"
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
    by error handlers are rolled back along with the failed view's writes. The
    middleware raises `RLSConfigurationError` if the mode is used under ASGI (see
    [Middleware](middleware.md#use_local_set-and-asgi)).

Outside a request (management commands, Celery tasks, scripts), you still provide the
transaction yourself:

```python
from django.db import transaction

with transaction.atomic():
    with tenant_context(tenant_id=42):
        orders = Order.objects.all()
```

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
- No cleanup is needed after the transaction ends.
- Pass `is_local=True` to `bypass_flag()`, `set_bypass_flag()` and `rls_context()` (inside
  `transaction.atomic()`): they are session-scoped by default in either mode, and nothing
  clears a flag that PgBouncer hands to another client.

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
- Then, on the thread that set the request's GUCs, the handler applies the request's GUCs
  as before (a connection checked out mid-request). A pooled connection always starts
  clean, and only the request's own thread gets the request's GUCs.
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

The GUCs set by the middleware and the tenant context managers are `SET LOCAL` values,
which end with the transaction (WSGI only; the middleware raises `RLSConfigurationError`
for this mode under ASGI). Bypass flags are not: `bypass_flag()`, `set_bypass_flag()` and
`rls_context()` default to `is_local=False` in either mode, so a flag whose block did not
restore it can still go back to the pool. System check `W010` does not fire in this mode;
the checkout clear still runs and covers those flags.

## System Checks

django-rls-tenants provides system checks for two pooling misconfigurations:

### W004: CONN_MAX_AGE with session GUCs

```
?: (django_rls_tenants.W004) DATABASES['default'] has CONN_MAX_AGE=600 with
USE_LOCAL_SET=False. This risks GUC state leaking between requests on persistent
connections. Consider USE_LOCAL_SET=True, which scopes the GUCs to a transaction the
middleware opens per request.
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
django_rls_tenants.pool.reset_rls_gucs, or set RLS_TENANTS['USE_LOCAL_SET'] = True
(WSGI only).
```

It fires once for every alias in `RLS_TENANTS["DATABASES"]` whose `OPTIONS["pool"]` is
enabled without a `reset` (the pool is `True`, or a dict without `"reset"` or with
`"reset": None`), when `USE_LOCAL_SET=False`. Any `reset`, including your own, silences it.
The checkout clear (see above) protects the next borrower either way; the check asks for
the return-side clear so idle connections hold no tenant state.

**Fix:** Either:

- Set the pool's `reset` to `django_rls_tenants.pool.reset_rls_gucs` (see
  [Clearing on Return](#clearing-on-return-reset_rls_gucs)), or
- Set `USE_LOCAL_SET = True` (WSGI only). Session-scoped bypass flags can still go back to
  the pool in that mode; `reset_rls_gucs` clears them too.

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
