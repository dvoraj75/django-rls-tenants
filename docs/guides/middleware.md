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

## How It Works

### Request Phase (`process_request`)

1. Checks if `request.user` exists and `is_authenticated`.
2. If **unauthenticated**: does nothing. RLS policies block all access (fail-closed).
3. If **authenticated**: reads the `TenantUser` protocol properties:
    - `is_tenant_admin`: if `True`, sets `rls.is_admin = 'true'` and clears `rls.current_tenant`.
    - `rls_tenant_id`: if not admin, sets `rls.current_tenant = str(tenant_id)` and `rls.is_admin = 'false'`.
4. Sets the internal `ContextVar` state for automatic query scoping (tenant users get
   auto-scoped queries; admin users and unauthenticated requests do not).
5. Marks a `ContextVar` flag that GUCs were set (used by the safety-net signal handler).

### Response Phase (`process_response`)

1. Clears the `ContextVar` auto-scope state to prevent cross-request leaks.
2. If `USE_LOCAL_SET` is `False` (default): clears both GUC variables to prevent
   cross-request leaks on persistent connections.
3. If `USE_LOCAL_SET` is `True`: GUCs are automatically cleared at transaction end
   (by PostgreSQL), so explicit cleanup is skipped.
4. Clears the `ContextVar` GUC flag.

### Exception Phase (`process_exception`)

If a view raises an unhandled exception, `process_response` may not run (depending
on middleware ordering). The `process_exception` handler ensures cleanup still happens:

1. Resets the `ContextVar` auto-scope state (via the saved token or fallback to `None`).
2. Clears GUC variables (same logic as `process_response`).

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
RLSTenantMiddleware.process_request()
    │
    ├── user.is_authenticated == False → no-op (fail-closed)
    │
    ├── user.is_tenant_admin == True
    │   └── SET rls.is_admin = 'true'
    │       CLEAR rls.current_tenant
    │       SET auto-scope state = None (no filter)
    │
    └── user.is_tenant_admin == False
        └── SET rls.current_tenant = str(tenant_id)
            SET rls.is_admin = 'false'
            SET auto-scope state = tenant_id
    │
    ▼
View executes (queries auto-scoped + filtered by RLS)
    │
    ▼
RLSTenantMiddleware.process_response()  (or process_exception on error)
    │
    ├── RESET auto-scope ContextVar (via saved token)
    ├── USE_LOCAL_SET == False → CLEAR both GUCs
    └── USE_LOCAL_SET == True  → no-op (transaction handles cleanup)
```

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
- Async views (GUC setting uses sync database calls)
- Any other Django-compatible request handler

The only requirement is that `request.user` satisfies the `TenantUser` protocol.

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

See [Context Managers](context-managers.md) for details.
