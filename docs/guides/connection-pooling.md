# Connection Pooling

When using a connection pooler like **PgBouncer** or **pgpool-II**, special care
is needed to prevent GUC variables from leaking between requests through pooled
connections.

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
    This is the same shape as `ATOMIC_REQUESTS`, with the same rules: catch database
    errors inside your own `transaction.atomic()` block or the rest of the request fails
    with "current transaction is aborted"; `transaction.on_commit()` callbacks run when
    the request ends; and `@non_atomic_requests` has no effect, because the middleware's
    transaction encloses the view anyway. A streaming response iterates its body after
    the transaction has ended, so queries made while streaming run without tenant
    context (fail-closed: no rows) -- establish a context inside the generator. The
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

## System Checks

django-rls-tenants provides a system check for the most common pooling misconfiguration:

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
