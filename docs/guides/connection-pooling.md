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
        "ATOMIC_REQUESTS": True,  # Required for SET LOCAL
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
| **Requires `ATOMIC_REQUESTS`** | No | Yes |
| **Nesting support** | Full save/restore | PostgreSQL handles it |

### Why ATOMIC_REQUESTS Is Required

`SET LOCAL` only works inside a transaction. With `ATOMIC_REQUESTS=True`, Django wraps
each view in `transaction.atomic()`, ensuring GUCs are active for the entire request
and automatically cleared at the end.

Without `ATOMIC_REQUESTS`, you must manually wrap all database operations:

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

django-rls-tenants provides system checks for common pooling misconfigurations:

### W003: USE_LOCAL_SET without ATOMIC_REQUESTS

```
(django_rls_tenants.W003) USE_LOCAL_SET is True but ATOMIC_REQUESTS is
not enabled for database 'default'. SET LOCAL requires an active transaction.
```

**Fix:** Set `ATOMIC_REQUESTS = True` in your database config, or ensure all database
access happens inside `transaction.atomic()` blocks.

### W004: CONN_MAX_AGE with session GUCs

```
(django_rls_tenants.W004) CONN_MAX_AGE is set to a non-zero value for
database 'default' and USE_LOCAL_SET is False. Session-scoped GUCs may
persist across requests on reused connections.
```

**Fix:** Either:

- Set `USE_LOCAL_SET = True` and `ATOMIC_REQUESTS = True`, or
- Set `CONN_MAX_AGE = 0` (Django default, closes connections after each request).

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
        "ATOMIC_REQUESTS": True,
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```
