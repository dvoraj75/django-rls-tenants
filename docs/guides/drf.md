# Django REST Framework

Django REST Framework (DRF) authenticates a request **inside the view**, in
`APIView.initial()`, after every Django middleware has run. A client that
authenticates with a token, a JWT or any other DRF authentication class therefore
reaches `RLSTenantMiddleware` as an `AnonymousUser`, and the middleware sets no
context. The view then reads zero rows (fail-closed), or raises
`NoTenantContextError` when `STRICT_MODE` is enabled.

django-rls-tenants ships a **native DRF integration** that opens the RLS context
where DRF knows the user: right after authentication, for the whole of the view.
It also refuses requests that would run outside that context, and fills the
tenant foreign key of a serializer from it.

!!! note "Session authentication"
    Users that DRF's `SessionAuthentication` serves are the exception: Django's
    `AuthenticationMiddleware` resolves them before `RLSTenantMiddleware` runs, so
    the middleware sets their context itself. The integration detects this and
    adds nothing on top (see [With `RLSTenantMiddleware`](#with-rlstenantmiddleware)).

## Install

The integration lives behind the `drf` extra, keeping Django REST Framework an
optional dependency (`djangorestframework>=3.16`, the first release that supports
Django 5.2):

```bash
pip install "django-rls-tenants[drf]"
```

Import it from `django_rls_tenants.contrib.drf` (never from the top-level package,
which stays DRF-free):

```python
from django_rls_tenants.contrib.drf import (
    CurrentTenantDefault,
    RLSTenantAPIViewMixin,
    RLSTenantModelSerializer,
    RLSTenantPermission,
    RLSTenantSerializerMixin,
)
```

Importing the module without DRF installed raises a clear `ImportError`.

## Quick start

A viewset, a serializer and two settings:

```python title="api.py"
from rest_framework import viewsets

from django_rls_tenants.contrib.drf import RLSTenantAPIViewMixin, RLSTenantModelSerializer

from .models import Order


class OrderSerializer(RLSTenantModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "product", "amount"]


class OrderViewSet(RLSTenantAPIViewMixin, viewsets.ModelViewSet):
    serializer_class = OrderSerializer

    def get_queryset(self):
        return Order.objects.all()   # scoped to request.user's tenant
```

```python title="settings.py"
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "django_rls_tenants.contrib.drf.RLSTenantPermission",
    ],
}
```

There is no `.filter(tenant=...)` and no `tenant=` on create: the mixin opens the
context, the manager and the database policy scope every query, and the serializer
stamps the tenant on writes.

!!! tip "See it running"
    The bundled
    [example project](https://github.com/dvoraj75/django-rls-tenants/tree/main/example)
    exposes a notes API at `/api/notes/` (`notes/api.py`) with token and session
    authentication, covered by `tests/test_api.py` under a non-superuser role. See
    its README for the `curl` steps.

## `RLSTenantAPIViewMixin`

The mixin works on any `APIView`: generic views and viewsets included (hence the
name). **List it before the DRF class.** Listed after it, `APIView.dispatch()`
runs instead of the mixin's, so the class raises `RLSConfigurationError` at class
creation rather than silently serving requests without a context.

### Lifecycle

```
request
  -> RLSTenantMiddleware        (AnonymousUser for a token client: sets nothing)
  -> RLSTenantAPIViewMixin.dispatch()
       -> initial() -> perform_authentication()    DRF authenticates the user
            -> context entered here
       -> permission and throttle checks            inside the context
       -> handler (list(), create(), ...)           inside the context
       -> DRF's exception handler, if it raised     inside the context
       -> response.render()                         inside the context
       -> context exited, also when the view raised
```

Right after DRF authenticates the request, the mixin enters `admin_context()` for a
user with `is_tenant_admin` and `tenant_context(user.rls_tenant_id)` for any other
user (the [`TenantUser` protocol](user-integration.md)). The context exits before
`dispatch()` returns, whether the view returns, raises a handled error (a 400, a
404) or an unhandled one (a 500).

| `request.user` | Context |
|----------------|---------|
| Unauthenticated | none entered: the view runs in the enclosing context, normally none (fail-closed: zero rows, or `NoTenantContextError` under `STRICT_MODE`); see the warning below |
| `is_tenant_admin` | `admin_context()` |
| Non-admin with a tenant | `tenant_context(user.rls_tenant_id)` |
| Non-admin without a tenant | `403 Forbidden` |

!!! warning "A request DRF did not authenticate keeps an enclosing context"
    The mixin enters nothing for a request whose `request.user` DRF left anonymous.
    The view then runs in whatever context encloses it. With `RLSTenantMiddleware`
    installed that can be the context of the **session cookie's** user, when the
    view's `authentication_classes` do not accept the session (a token-only view,
    say). DRF views are exempt from Django's CSRF middleware, and only
    `SessionAuthentication` enforces CSRF, so an `AllowAny` token-only view could
    serve a cross-site request in the cookie user's tenant. `RLSTenantPermission`
    refuses such a request (no authenticated user). Keep it in the default
    permission classes, and list `SessionAuthentication` in every view that the
    middleware's users reach.

The 403 for a tenantless account carries a generic detail ("Your account is not
assigned to a tenant."). The hint that names the `TenantUser` attributes goes to the
`django_rls_tenants` logger as a warning, not to the client.

### The response is rendered inside the context

DRF renders a `Response` after `dispatch()` would normally have returned. The mixin
calls `response.render()` inside the context before it exits, so everything that is
evaluated lazily while rendering is scoped too:

- an unevaluated queryset (or a generator) passed to `Response(...)`;
- the browsable API's HTML forms and their related-field dropdowns, which would
  otherwise list no related objects, or every tenant's.

One exception: with `ATOMIC_REQUESTS=True`, DRF's exception handler marks the request
transaction for rollback on a handled error (a 400, 403 or 404), and Django refuses
every query in it until it ends. The mixin then leaves the response to Django, which
renders it after the rollback, **outside** the context. A JSON error renders as
usual; a browsable-API error page shows its forms' related-field dropdowns empty
(fail-closed), or fails with `NoTenantContextError` under `STRICT_MODE`.

!!! note "`process_template_response` middleware"
    Because the mixin has already rendered the response, a Django middleware's
    `process_template_response()` can no longer change a DRF response served by a
    view that entered a context. `process_response()` still sees the rendered
    response as usual.

### Choosing the context: `get_rls_context()`

`get_rls_context(request)` is called once per request, after authentication. It
returns the context manager to run the request in, or `None` to enter none. Override
it to pick the context yourself, for example to let an admin act on one tenant
selected with a header:

```python
from django_rls_tenants import tenant_context
from django_rls_tenants.contrib.drf import RLSTenantAPIViewMixin


class TenantHeaderMixin(RLSTenantAPIViewMixin):
    def get_rls_context(self, request):
        selected = request.headers.get("X-Tenant")
        if selected and request.user.is_tenant_admin:
            return tenant_context(int(selected))
        return super().get_rls_context(request)
```

Only a tenant admin may choose a tenant: any other user would be able to read
another tenant's rows. Validate the value, and keep the `super()` fallback so that
every other request gets its own context.

### A subclass that overrides `perform_authentication()`

The mixin enters the context at the end of `perform_authentication()`. A view that
overrides it **without calling `super()`** (to authenticate lazily, say) therefore
enters no context, and is fail-closed. Call `super().perform_authentication(request)`
if the view must run in the user's context.

## `RLSTenantPermission`

A permission class that turns a misconfigured view into an error instead of an
empty list.

`has_permission()` passes only for an authenticated user inside an active RLS
context that is exactly the user's own: a non-admin user needs their own tenant's
context (never another tenant's, never the admin bypass); a tenant admin passes in
any context. A view that forgot the mixin, served to a token client, so answers
`403` (an anonymous client gets `401`) instead of `200` with an empty list.

`has_object_permission()` checks that the object's tenant foreign key
(`RLS_TENANTS["TENANT_FK_FIELD"]`) equals the active tenant. Any object passes in
the admin context, and so does an object without that field (it is not
tenant-scoped). PostgreSQL already filters what the view can read, so this is
defense in depth for an object obtained another way.

List it in `REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]`, after `IsAuthenticated`
(see the [quick start](#quick-start)), or per view:

```python
class OrderViewSet(RLSTenantAPIViewMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, RLSTenantPermission]
```

Listing it in the settings is safe: the module avoids importing
`rest_framework.views` at import time, because DRF imports the default permission
classes while it builds `APIView`.

## Serializers

`RLSTenantModelSerializer` (or `RLSTenantSerializerMixin`, mixin first, for your own
base) hides the tenant foreign key and fills it from the active context:

```python
from rest_framework import serializers

from django_rls_tenants.contrib.drf import RLSTenantSerializerMixin


class OrderSerializer(RLSTenantSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "product", "amount"]
```

In a tenant context (and with no context), the field named by
`RLS_TENANTS["TENANT_FK_FIELD"]` becomes
`HiddenField(default=CurrentTenantDefault())`, **whatever `Meta` says**: it is added
even when `Meta.fields` does not list it.

- It is never in the output.
- Any other writable field that would write the tenant foreign key is removed: a
  declared `tenant_id` field, or one with `source="tenant"`. Read-only fields that
  read it, such as `CharField(source="tenant.name", read_only=True)`, stay.
- A `tenant` value in the payload is **ignored** (DRF `HiddenField` semantics, like
  `CurrentUserDefault`), so a client cannot write into another tenant.
- Create and update set it to the active tenant; a partial update leaves it alone.
- A `UniqueConstraint` that includes the tenant FK is still validated: DRF builds a
  `UniqueTogetherValidator` for it, so a duplicate is a `400`, not an
  `IntegrityError`.

In the **admin context** the serializer is left as declared. List the field in
`Meta.fields` so that an admin can choose the tenant (this mirrors
[`RLSTenantModelAdmin`](admin.md)); leave it out and an admin create has no tenant to
write. A model without the tenant field is left alone.

The fields are built on first access, inside the view, so the context a serializer
sees is the request's. Construct a serializer outside a request (in a shell or a
test) inside `tenant_context()`.

### `CurrentTenantDefault`

The field default, usable on your own `HiddenField`:

```python
tenant = serializers.HiddenField(default=CurrentTenantDefault())
```

It returns an instance of `RLS_TENANTS["TENANT_MODEL"]` that holds only its primary
key (the other fields are deferred), so filling the field costs no query. It raises
`NoTenantContextError` in the admin context or with no context, because neither has
a tenant.

## Querysets

Build the queryset per request:

```python
def get_queryset(self):
    return Order.objects.all()
```

A class-level `queryset = Order.objects.all()` still isolates tenants, but it is
built at import, outside any context, so the manager adds no ORM-level tenant filter
to it: it relies on the RLS policy alone, without the `WHERE tenant_id = X` the
planner can use (see [Performance & Indexing](performance.md)). DRF re-evaluates a
class-level queryset per request, so nothing leaks; you just lose the second layer.

## With `RLSTenantMiddleware`

With `RLSTenantMiddleware` installed, a session-authenticated user already has the
context from the middleware. When the active context equals the one the user
implies, the mixin enters nothing, writes no extra GUCs, and Django renders the
response inside the middleware's context. A token client reaches the mixin with no
context and gets the mixin's.

Keep the middleware: it covers the rest of your project (templates, the Django
admin, non-DRF views), which the mixin does not. The two are complementary, not
alternatives.

### Router root and schema views

`RLSTenantPermission` in the default permission classes also applies to DRF's own
views that have no mixin: `DefaultRouter`'s API root (`/api/`) and the schema views.
A token client then gets a `403` there. Give the router a root view with the mixin:

```python
from rest_framework import routers

from django_rls_tenants.contrib.drf import RLSTenantAPIViewMixin


class APIRootView(RLSTenantAPIViewMixin, routers.APIRootView):
    pass


class Router(routers.DefaultRouter):
    APIRootView = APIRootView
```

or set `permission_classes` on the schema view explicitly.

If a view serves token clients, put `RLSTenantPermission` in the default permission
classes so that a view without the mixin answers 403 instead of an empty list.

## `STRICT_MODE`

With `STRICT_MODE=True`, a token request to a view **without** the mixin raises
`NoTenantContextError` (a 500) the first time it queries an RLS model, instead of
returning an empty list. With `RLSTenantPermission` in the default permission
classes, such a request is refused with a 403 before it reaches a query. Inside the
mixin's context nothing changes: the context counts as an active context.

## `USE_LOCAL_SET` and `ATOMIC_REQUESTS`

The integration works in both GUC modes.

- `USE_LOCAL_SET=False`: the mixin sets session GUCs and clears them when it exits.
- `USE_LOCAL_SET=True`: with `ATOMIC_REQUESTS=True` the view runs inside the request
  transaction and the mixin's `SET LOCAL` lands in it. Without it the mixin writes
  nothing and the [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard)
  applies the context to each statement. See
  [Code Outside a Request Transaction](connection-pooling.md#code-outside-a-request-transaction).

The mixin opens **no transaction of its own**. (The middleware's request
transaction on the sync path exists only for requests it set a context for, which
a token request is not.) Set `ATOMIC_REQUESTS=True` if an API write should be
atomic.

## ASGI

DRF views are synchronous. Under ASGI, Django runs a sync view in one
thread-sensitive hop, so the mixin enters and exits the context on one thread and
connection. The integration is tested with Django's `AsyncClient`. See
[Async Deployment](async.md) for the server and pooling settings.

## Why there is no authentication class

An authentication class that set the context would look natural, but DRF's
authenticators return `(user, auth)` and are never called back when the request
ends. Such a class could enter a context and never exit it. With the GUC affinity
guard, a context left open follows the thread into the **next** request, which is
exactly the leak the library exists to prevent. The mixin owns both the entry
(`perform_authentication()`) and the exit (`dispatch()`), so it cannot leak.

## Limitations

- The mixin must be listed before the DRF class (`RLSConfigurationError` otherwise).
- A view that overrides `perform_authentication()` without calling `super()` enters
  no context.
- A class-level `queryset` skips the ORM-level tenant filter; use `get_queryset()`.
- A `process_template_response()` middleware cannot change a DRF response that the
  mixin has rendered.
- Under `ATOMIC_REQUESTS=True` a handled error is rendered after the rollback,
  outside the context (see [above](#the-response-is-rendered-inside-the-context)).
- A request DRF did not authenticate keeps an enclosing context, such as the
  middleware's for a session cookie (see the warning under [Lifecycle](#lifecycle)).
- The tenant foreign key is the one named by `RLS_TENANTS["TENANT_FK_FIELD"]`, and
  `CurrentTenantDefault` expects it to reference the tenant's primary key.
- DRF's `ModelSerializer` does not call `Model.full_clean()`, so model validators
  and `clean()` do not run on API writes (see [Security](../advanced/security.md)).
  Use serializer validators, or call `full_clean()` yourself in `perform_create()`.
- Only DRF code is covered. A plain Django view inside the same project still needs
  `RLSTenantMiddleware`, or an explicit context manager.
