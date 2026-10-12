# Middleware

`RLSTenantMiddleware` is the primary integration point for web applications. It
automatically sets the RLS context for each request based on the authenticated user.

## Setup

Add the middleware to your `MIDDLEWARE` setting, **after** `AuthenticationMiddleware`:

```python title="settings.py"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must come after AuthenticationMiddleware:
    "django_rls_tenants.RLSTenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
```

!!! important
    The middleware reads `request.user`, which is set by `AuthenticationMiddleware`.
    Placing it before authentication will result in no tenant context being set.

When `USE_LOCAL_SET=True`, also place middleware implementing `process_exception`
**before** `RLSTenantMiddleware`. Django calls exception hooks in reverse order and
stops when one returns a response. The RLS hook must run first to record the failed
view and roll back its writes. An unsafe ordering raises `RLSConfigurationError`
when the middleware is loaded. Middleware classes without exception hooks can still
follow it. Function-based middleware factories must precede it too: their returned
instances can expose exception hooks that cannot be inspected before construction.

## How It Works

### Request Phase (`process_request`)

1. Checks if `request.user` exists and `is_authenticated`.
2. If **unauthenticated**: does nothing. RLS policies block all access (fail-closed).
3. If **authenticated**: reads the `TenantUser` protocol properties:
    - `is_tenant_admin`: if `True`, sets `rls.is_admin = 'true'` and clears `rls.current_tenant`.
    - `rls_tenant_id`: if not admin, sets `rls.current_tenant = str(tenant_id)` and `rls.is_admin = 'false'`.
4. Sets GUCs on **all database aliases** listed in `RLS_TENANTS["DATABASES"]` (default:
   `["default"]`). If setting GUCs on one alias fails, all previously-set aliases are
   cleaned up before the exception propagates.
5. Sets the internal `ContextVar` state for automatic query scoping (tenant users get
   auto-scoped queries; admin users and unauthenticated requests do not).
6. Sets `_rls_context_active=True` to mark that an RLS context is active. This is used
   by strict mode to distinguish "middleware set context" from "no context at all".
7. Marks a `ContextVar` flag that GUCs were set (used by the safety-net signal handler).

### Transaction Per Request (`USE_LOCAL_SET=True`)

With `RLS_TENANTS["USE_LOCAL_SET"] = True` the GUCs are set with `SET LOCAL`, which only
exists inside a transaction. Django's `ATOMIC_REQUESTS` wraps the view callable only, so
it cannot cover `process_request`. The middleware therefore opens the transaction
itself: for every authenticated request it enters `transaction.atomic(using=alias)` for
each alias in `RLS_TENANTS["DATABASES"]` *before* `process_request` and leaves it
*after* `process_response`. The GUCs live exactly as long as the request and vanish at
the commit (or rollback) that ends it. Unauthenticated requests get no transaction.

Transaction behavior and limitations:

- A view that **raises** rolls the whole request transaction back. The middleware
  records the failure in `process_exception`, then marks each transaction for rollback
  just before leaving it. This lets error handlers query after non-database exceptions
  such as `Http404`; the tenant context stays active until response cleanup. Writes by
  those handlers are rolled back too. A view that **returns** an error response without
  raising commits. These view outcomes match `ATOMIC_REQUESTS`, but error handling and
  downstream middleware also run inside this transaction.
- A database error must be caught inside your own `transaction.atomic()` block, or the
  rest of the request (including error handlers) cannot query the broken transaction.
  Keeping `ATOMIC_REQUESTS=True` provides a savepoint around the view, allowing error
  handlers to query after a database exception escapes that view.
- `transaction.on_commit()` callbacks run when the request finishes.
- `@non_atomic_requests` has no effect -- the middleware's transaction encloses the view.
- Keeping `ATOMIC_REQUESTS=True` is harmless; Django's per-view transaction becomes a
  savepoint inside the middleware's.
- A `StreamingHttpResponse` iterates its body after the transaction has ended, so
  queries made while streaming see no tenant context (fail-closed). Open a context
  inside the synchronous generator and evaluate the queries there. With
  `USE_LOCAL_SET=True`, first enter `transaction.atomic(using=alias)`, then
  `tenant_context(tenant_id, using=alias)` (or `admin_context(using=alias)`), using
  the database alias queried by the generator.

With the default `USE_LOCAL_SET=False` nothing changes: the view runs in autocommit (or
under `ATOMIC_REQUESTS`, if you enable it) exactly as before.

### `USE_LOCAL_SET` and ASGI

The async middleware path cannot hold a synchronous `transaction.atomic()` open across
the awaited downstream chain, so `USE_LOCAL_SET=True` is not available under ASGI. The
middleware raises `RLSConfigurationError` for authenticated requests in that setup
instead of failing deep inside `SET LOCAL`. Serve the application through WSGI, or use
the default session-scoped mode. Native async support is tracked for v1.4.0.

### Running under ASGI

Under ASGI, Django runs the middleware's hooks through `sync_to_async` on the request's
thread-sensitive connection (each ASGI request gets its own). The GUCs, auto-scope state
and strict-mode flag they set are visible to async views. Each hook runs in its own copy
of the request's `contextvars.Context`. Since 1.3.4, cleanup clears the `ContextVar`s
when their tokens belong to another context instead of failing (#58).
Cleanup also runs if an async view is cancelled before the response or exception hooks
run. Keep one RLS context per request task: do not `asyncio.gather` work for different
tenants inside one request, since it shares the request's connection. Async views that
need to change the context use `async_tenant_context()` / `async_admin_context()`
(1.4.0+, see [Async Code](context-managers.md#async-code)). A native async middleware path
is tracked in [#38](https://github.com/dvoraj75/django-rls-tenants/issues/38).

### Response Phase (`process_response`)

1. Resets the `_rls_context_active` flag (via saved token or, under ASGI where the token
   belongs to another `contextvars.Context`, by clearing the variable, #58) to prevent
   cross-request leaks.
2. Clears the `ContextVar` auto-scope state to prevent cross-request leaks.
3. If `USE_LOCAL_SET` is `False` (default): clears both GUC variables on **all
   configured database aliases** to prevent cross-request leaks on persistent connections.
4. If `USE_LOCAL_SET` is `True`: GUCs are automatically cleared at transaction end
   (by PostgreSQL), so explicit cleanup is skipped.
5. Clears the `ContextVar` GUC flag.

### Exception Phase (`process_exception`)

If a view raises an unhandled exception, `process_response` may not run (depending
on middleware ordering). The `process_exception` handler ensures cleanup still happens:

1. Resets the `_rls_context_active` flag (via saved token or fallback to `False`; under
   ASGI, where the token belongs to another `contextvars.Context`, by clearing the
   variable, #58).
2. Resets the `ContextVar` auto-scope state (via the saved token or fallback to `None`;
   cleared the same way under ASGI).
3. Clears GUC variables (same logic as `process_response`). The GUCs are cleared even if
   resetting a `ContextVar` fails.

This prevents `ContextVar` leaks that could affect subsequent requests on the same
thread (WSGI) or async task (ASGI).

### Error Handling

If setting a GUC fails during `process_request` (e.g., broken database connection):

1. The `ContextVar` state is reset to `None`.
2. Both GUCs are cleared on a best-effort basis.
3. The exception is re-raised (Django returns a 500 response).
4. This prevents partial GUC state from leaking to the next request.

## Request Lifecycle Diagram

```
Request arrives
    │
    ▼
AuthenticationMiddleware
    │ sets request.user
    ▼
RLSTenantMiddleware.__call__()
    │ USE_LOCAL_SET == True and authenticated → BEGIN (atomic per alias)
    ▼
RLSTenantMiddleware.process_request()
    │
    ├── user.is_authenticated == False → no-op (fail-closed)
    │
    ├── user.is_tenant_admin == True
    │   └── SET rls.is_admin = 'true'
    │       CLEAR rls.current_tenant
    │       SET auto-scope state = None (no filter)
    │       SET _rls_context_active = True
    │
    └── user.is_tenant_admin == False
        └── SET rls.current_tenant = str(tenant_id)
            SET rls.is_admin = 'false'
            SET auto-scope state = tenant_id
            SET _rls_context_active = True
    │
    ▼
View executes (queries auto-scoped + filtered by RLS)
  (strict mode: queries pass because _rls_context_active = True)
    │
    ▼
RLSTenantMiddleware.process_response()  (or process_exception on error)
    │
    ├── RESET _rls_context_active (via saved token; cleared under ASGI)
    ├── RESET auto-scope ContextVar (via saved token; cleared under ASGI)
    ├── USE_LOCAL_SET == False → CLEAR both GUCs
    └── USE_LOCAL_SET == True  → no-op, then COMMIT/ROLLBACK drops the SET LOCAL values
```

## Multi-Database Support

By default, the middleware sets GUCs only on the `default` database connection.
In multi-database setups (read replicas, analytics databases), configure all
aliases that serve RLS-protected queries:

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "DATABASES": ["default", "replica"],
}
```

The middleware sets GUCs on all configured aliases during `process_request` and
clears them during `process_response`. A `connection_created` signal handler
also sets GUCs on lazily-created connections that don't exist when the middleware
runs (e.g., a replica connection opened by a database router mid-request).

See [Configuration](../getting-started/configuration.md#databases) for details.

## Safety Net

django-rls-tenants connects to Django's `request_finished` signal as a safety net.
If the middleware's `process_response` is somehow skipped (e.g., due to an unhandled
exception in another middleware), the signal handler clears the GUC variables.

This is a defense-in-depth measure -- the primary cleanup always happens in
`process_response`.

## API-Agnostic Design

The middleware is API-agnostic. It works identically for:

- Django views and templates
- Django REST Framework
- GraphQL (Graphene, Strawberry, Ariadne)
- Async views and ASGI servers (the hooks run through `sync_to_async`; `USE_LOCAL_SET=True`
  is WSGI-only)
- Any other Django-compatible request handler

The only requirement is that `request.user` satisfies the `TenantUser` protocol.

## Strict Mode

The middleware sets `_rls_context_active=True` for authenticated requests, which
satisfies the strict mode check. Unauthenticated requests do **not** set this
flag -- if strict mode is enabled, queries in unauthenticated views will raise
`NoTenantContextError` rather than silently returning zero rows.

This is the intended behavior: strict mode surfaces missing context at the point
of query execution, making it easier to identify views that should require
authentication.

## Debug Logging

The middleware emits `DEBUG`-level log messages when GUCs are set and cleared.
This is useful for tracing RLS context in development or diagnosing issues in
production. No output is produced unless you enable `DEBUG` level on the
`django_rls_tenants` logger:

```python title="settings.py"
LOGGING = {
    "version": 1,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django_rls_tenants": {
            "handlers": ["console"],
            "level": "DEBUG",
        },
    },
}
```

Example output:

```
DEBUG Middleware set GUCs on databases=['default'] for user=User (admin=False, tenant=42)
DEBUG Middleware cleared GUCs on databases=['default']
```

## Using Without Middleware

For non-web contexts (management commands, Celery tasks, scripts), use context managers
instead of middleware:

```python
from django_rls_tenants import tenant_context, admin_context

# In a management command:
with tenant_context(tenant_id=42):
    orders = Order.objects.all()

# In a Celery task:
with admin_context():
    all_users = User.objects.all()
```

See [Context Managers](context-managers.md) and [Celery Tasks](celery-tasks.md) for details.
