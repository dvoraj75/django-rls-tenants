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

The phases below are the synchronous (WSGI) path. Under ASGI the middleware does the same
work in its own async path; see [Running under ASGI](#running-under-asgi).

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
7. Records the GUC values it set for the
   [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard), which applies them
   on any other connection the request's code uses (*added in 1.4.0*).
8. Records in a `ContextVar` that GUCs were set, and on which thread (read by the
   safety-net and `connection_created` signal handlers).

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
the default session-scoped mode. The native async path added in 1.4.0 has the same limit.

### Running under ASGI

*Changed in 1.4.0.* When every middleware listed after it can run asynchronously (Django's
own middleware can), Django serves the request through the middleware's own async path,
`__acall__`, instead of the generic one inherited from `MiddlewareMixin`:

1. **One** `sync_to_async(..., thread_sensitive=True)` hop reads `request.user` and sets the
   GUCs on every alias in `RLS_TENANTS["DATABASES"]`. Django has no asynchronous database
   cursor, so this is a thread hop, not a non-blocking call. It runs on the request's
   thread-sensitive connection, the one the async ORM uses.
2. The auto-scope state and the strict-mode flag are set in the request coroutine itself, so
   async views, the async ORM and every later hop see them.
3. When the chain returns, raises, or the request task is cancelled, the `ContextVar`s are
   reset with their own tokens and **one** more hop clears the GUCs.

Because the tenant state is set and reset in one `contextvars.Context`, the
`Token was created in a different Context` failure (#58) cannot occur, and a context that was
active around the middleware is restored rather than cleared. `request.user` is read exactly
as on the synchronous path, so an authentication middleware that replaces `request.user`
keeps working; async views can still call `await request.auser()`.

If a view raises, Django calls `process_exception` in its own hop and then builds the error
response on a different thread and database connection. The hook ends the request context
first, so error pages run fail-closed, as under WSGI. If a synchronous-only middleware is
listed after `RLSTenantMiddleware`, Django runs `RLSTenantMiddleware` on its synchronous path
inside a hop instead, which works too.

Some failures never reach `process_exception`: a URL that does not resolve (Django resolves
it outside the code that calls the hook), a view that returns `None`, or an exception from a
`process_view` or `process_template_response` hook or from an async-capable middleware listed
after `RLSTenantMiddleware`. Django builds those error responses on a worker thread as well,
while the request context is still active. *Changed in 1.4.0:* the worker thread's connection
is cleared when it opens, and the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) applies the request's
context to each statement the error page runs on it, so the page sees what the request may
see (#41). Before the first statement run without that context, the guard takes it off the
connection again, so a later error page built on the same thread, an anonymous visitor's
included, sees nothing (#130). Before 1.4.0 the worker thread's connection got the request's
GUCs only if it opened during that request, and then kept them for the life of the thread.
Under WSGI, and for an exception from a synchronous-only middleware listed after
`RLSTenantMiddleware`, Django builds the error response on the request's own thread, inside
its context.

The same applies to database work handed to another thread with a copy of the request's
context, under ASGI or WSGI: `sync_to_async(..., thread_sensitive=False)`,
`asyncio.to_thread()`, or a function run with `contextvars.copy_context().run`. It runs on
that thread's own connection, and the guard applies the request's context there before each
statement. With `DJANGO_ALLOW_ASYNC_UNSAFE`, so do queries an async view runs on the
event-loop thread itself. Work started without a copy of the context
(`ThreadPoolExecutor.submit()`, `loop.run_in_executor()`) runs without one, fail-closed; open
`tenant_context()` / `admin_context()` inside it if it needs tenant data.

The async path does not call `process_request` or `process_response`. A subclass that
overrides either hook therefore keeps the hook-based path that 1.3.4 used, so its hooks
still run under ASGI. Each hook then costs its own hop, and cleanup clears the `ContextVar`s
rather than restoring an outer context.

Async views that need a different context use `async_tenant_context()` /
`async_admin_context()` (see [Async Code](context-managers.md#async-code)); on exit they
restore the middleware's context. Sibling tasks of one request can hold different contexts:
they share the request's connection, and the guard applies each task's context before each of
its statements (see [Concurrent tasks](context-managers.md#concurrent-tasks) for the limits).

A `StreamingHttpResponse` body is iterated after the middleware has finished, as under WSGI,
so queries made while streaming see no tenant context (fail-closed). Open
`async with async_tenant_context(...)` inside an async generator, or `tenant_context()` inside
a synchronous one.

### Response Phase (`process_response`)

1. Ends the GUC values recorded for the GUC affinity guard, first, so that it does not
   apply the request's context again after the cleanup (*added in 1.4.0*).
2. Resets the `_rls_context_active` flag (via saved token or, when the token belongs to
   another `contextvars.Context`, by clearing the variable, #58) to prevent cross-request
   leaks.
3. Clears the `ContextVar` auto-scope state to prevent cross-request leaks. An
   unauthenticated request served inside a context block (a test that wraps the test
   client in `tenant_context()`, say) leaves that block's values alone (*changed in
   1.4.0*).
4. If `USE_LOCAL_SET` is `False` (default): clears both GUC variables on **all
   configured database aliases** to prevent cross-request leaks on persistent connections.
5. If `USE_LOCAL_SET` is `True`: GUCs are automatically cleared at transaction end
   (by PostgreSQL), so explicit cleanup is skipped.
6. Clears the `ContextVar` GUC flag.

The same cleanup runs when a `BaseException` (`KeyboardInterrupt`, a server's timeout)
escapes the view and skips `process_response` (*changed in 1.4.0*): under WSGI the
thread's next request would otherwise start inside this request's context.

### Exception Phase (`process_exception`)

If a view raises an unhandled exception, `process_response` may not run (depending
on middleware ordering). The `process_exception` handler ensures cleanup still happens.
Under ASGI it runs in Django's own hop, before the error response is built, and the
request's tokens stay with `__acall__`, so it clears the variables:

1. Resets the `_rls_context_active` flag (via saved token or fallback to `False`; when the
   token belongs to another `contextvars.Context`, or under ASGI, by clearing the
   variable, #58).
2. Resets the `ContextVar` auto-scope state (via the saved token or fallback to `None`;
   cleared the same way).
3. Clears GUC variables (same logic as `process_response`). The GUCs are cleared even if
   resetting a `ContextVar` fails.
4. Ends a context block the view left open, such as a generator suspended inside
   `async_admin_context()`, so the error response is not built in that block's context
   (*added in 1.4.0*). A `got_request_exception` receiver does the same for a 500
   response that `process_exception` does not see (a view that returns `None`, an
   exception from a later middleware), and the end of the request ends such a block too.
   A 4xx response Django builds for an exception raised outside the view (`Http404` from
   a later middleware, say) sends no signal, and an exception middleware listed after
   `RLSTenantMiddleware` that returns a response stops this hook from running (Django
   calls `process_exception` innermost first): list such middleware before
   `RLSTenantMiddleware`. With `USE_LOCAL_SET=True` the block's `SET LOCAL` values stay
   until the transaction ends. Keep context blocks inside the code that needs them rather
   than in a generator the view leaves suspended. A block that a middleware listed after
   `RLSTenantMiddleware` holds around the rest of the chain is ended as well when a 500
   response is built inside it, so the rest of that block runs without a context.

This prevents `ContextVar` leaks that could affect subsequent requests on the same
thread (WSGI) or async task (ASGI).

### Error Handling

If setting a GUC fails during `process_request` (e.g., broken database connection):

1. The `ContextVar` state is reset to `None`.
2. Both GUCs are cleared on a best-effort basis.
3. The exception is re-raised (Django returns a 500 response).
4. This prevents partial GUC state from leaking to the next request.

## Request Lifecycle Diagram

The synchronous (WSGI) path. Under ASGI, `__acall__` does the request phase in one hop and
the cleanup in one hop (see [Running under ASGI](#running-under-asgi)).

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
    ├── RESET _rls_context_active (via saved token)
    ├── RESET auto-scope ContextVar (via saved token)
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
runs (e.g., a replica connection opened by a database router mid-request), on the
thread that set the request's GUCs. *Changed in 1.4.0:* the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) also applies the
active context on every configured alias before each statement, on any thread (see
[Running under ASGI](#running-under-asgi)), whichever alias the context was entered on:
a `tenant_context(...)` entered on `default` inside a request scopes a configured replica
too. A connection checked out of Django's connection pool is cleared of every RLS GUC
first (see [Django's Connection Pool](connection-pooling.md#djangos-connection-pool)).

See [Configuration](../getting-started/configuration.md#databases) for details.

## Safety Net

django-rls-tenants connects to Django's `request_finished` signal as a safety net.
If the middleware's `process_response` is somehow skipped (e.g., due to an unhandled
exception in another middleware), the signal handler clears the GUC variables.

This is a defense-in-depth measure -- the primary cleanup always happens in
`process_response`. Under ASGI the primary cleanup is the `finally` block of `__acall__`,
which also runs for failed and cancelled requests, so the safety net only matters if that
GUC clear itself fails. It sees the middleware's flag only when Django sends
`request_finished` from the request's own task: Django 6.1 does, Django 5.2 and 6.0 do not.
It skips a closed connection: with Django's connection pool, Django has always returned the
request's connection by then, and the clear on its next checkout (and the pool's `reset`
callback, if one is set) cleans it.

## API-Agnostic Design

The middleware is API-agnostic. It works identically for:

- Django views and templates
- Django REST Framework
- GraphQL (Graphene, Strawberry, Ariadne)
- Async views and ASGI servers (a native async path, see
  [Running under ASGI](#running-under-asgi); `USE_LOCAL_SET=True` is WSGI-only)
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
