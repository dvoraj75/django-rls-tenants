# Django Ninja

Django Ninja authenticates a request **inside the operation**, with the `auth=`
callbacks, after every Django middleware has run. A client that authenticates with a
bearer token, an API key or any other Ninja authentication therefore reaches
`RLSTenantMiddleware` as an `AnonymousUser`, and the middleware sets no context. The
operation then reads zero rows (fail-closed), or raises `NoTenantContextError` when
`STRICT_MODE` is enabled.

django-rls-tenants ships a **native Django Ninja integration** that opens the RLS
context where Ninja knows the user, for the whole of the operation. It can refuse
requests that would run outside that context, and it writes a payload to a model with
the tenant taken from the context.

!!! note "Session authentication"
    A user that Django's `AuthenticationMiddleware` resolves (`django_auth`, a session
    cookie) is set up by `RLSTenantMiddleware` before Ninja runs. When the active
    context already is the user's, the API enters nothing.

## Install

The integration lives behind the `ninja` extra, keeping Django Ninja an optional
dependency (`django-ninja>=1.5`, the first release with `add_decorator`):

```bash
pip install "django-rls-tenants[ninja]"
```

Import it from `django_rls_tenants.contrib.ninja` (never from the top-level package,
which stays Ninja-free):

```python
from django_rls_tenants.contrib.ninja import (
    RLSTenantGuard,
    RLSTenantModelSchema,
    RLSTenantNinjaAPI,
    arls_save,
    rls_save,
)
```

Importing the module without Django Ninja installed raises a clear `ImportError`.
Unlike DRF's `DEFAULT_PERMISSION_CLASSES`, no Ninja setting names a class that Ninja
imports while its own modules load, so there is no import-order trap: the module
imports Ninja at module level and needs configured settings and the app registry.

## Quick start

```python title="api.py"
from ninja import Router
from ninja.security import HttpBearer

from django_rls_tenants.contrib.ninja import (
    RLSTenantGuard,
    RLSTenantModelSchema,
    RLSTenantNinjaAPI,
    rls_save,
)

from .models import Order


class TokenAuth(HttpBearer):
    def authenticate(self, request, token):
        return lookup_user(token)  # a TenantUser, or None


class OrderIn(RLSTenantModelSchema):
    class Meta:
        model = Order
        fields = "__all__"  # the tenant field is left out


class OrderOut(RLSTenantModelSchema):
    class Meta:
        model = Order
        fields = "__all__"


router = Router()


@router.get("/orders/", response=list[OrderOut])
def list_orders(request):
    return Order.objects.all()


@router.post("/orders/", response=OrderOut)
def create_order(request, payload: OrderIn):
    return rls_save(request, Order(), payload)


api = RLSTenantNinjaAPI(auth=TokenAuth(), rls_guard=RLSTenantGuard())
api.add_router("/", router)
```

## `RLSTenantNinjaAPI`

A `NinjaAPI` subclass; use it instead of `NinjaAPI`. It takes `rls_guard=` (see
[`RLSTenantGuard`](#rlstenantguard)) besides the `NinjaAPI` arguments.

### Lifecycle

For each operation, Ninja runs `run()`: authentication (`auth=`), throttling, input
parsing and validation, the view function, then serialization of the result and
rendering of any error. The API enters the context **after** authentication,
throttling and input parsing, right before the view function:

- a user with `is_tenant_admin` gets `admin_context()`;
- any other user gets `tenant_context(user.rls_tenant_id)`;
- a non-admin user without a tenant gets a 403 (`"Your account is not assigned to a
  tenant."`; the hint naming the `TenantUser` attributes goes to the log);
- a request an `auth=` callback authenticated, but for which `get_rls_user()` names no
  user (the callback returned something that is not a `TenantUser`; see
  [The user](#the-user)), runs with **no RLS context**, whatever context encloses it,
  `RLSTenantMiddleware`'s for the session cookie included: it reads no protected row,
  and `STRICT_MODE` raises (`RLSTenantGuard` refuses it with a 401);
- a request no `auth=` callback authenticated and without a session user enters
  nothing: the operation runs in whatever context encloses it, none unless other code
  set one (`RLSTenantGuard` refuses it with a 401).

A request whose `auth=` callback fails gets Ninja's 401 before anything is entered.

The context stays active for the operation, the serialization of what it returns (a
lazy queryset included) and the rendering of an error, and is exited before the
response leaves the API, also when the operation raises. Input parsing runs before the
context is entered, so it runs outside it. The API covers every router mounted on it.

Both `def` and `async def` operations are supported. An `async def` operation gets
`async_admin_context()` / `async_tenant_context()`, entered in the request's
coroutine; the user lookup and the session read run in one thread-sensitive
`sync_to_async` hop. Under ASGI, Ninja runs a whole `def` operation (authentication,
view, serialization) in one thread-sensitive `sync_to_async` hop, and the API enters
the synchronous context there.

An `async def` operation may return a queryset (also in `Status(code, queryset)` or
Ninja's deprecated `(code, queryset)` tuple).
Ninja serializes the result of an `async def` operation in the event loop, where
evaluating a queryset raises `SynchronousOnlyOperation`, so the API evaluates the
returned queryset inside the context first, through Django's async iteration (one
thread-sensitive hop), and Ninja serializes the rows it fetched. The evaluation is the
queryset's own: a `for_user()` queryset keeps its scope, `prefetch_related()` its
prefetches (with their tenant filter) and `STRICT_MODE` applies. The serialization
itself (schema resolvers included) runs inside the context, but still in the event
loop: a relation it reaches lazily (`order.customer.name` for a foreign key that was
not fetched) raises `SynchronousOnlyOperation`. Fetch what the schema shows with
`select_related()` / `prefetch_related()`:

```python
@router.get("/orders/", response=list[OrderOut])
async def list_orders(request):
    return Order.objects.select_related("customer")
```

Only a queryset returned as it is, or as the body of a `Status` / `(code, body)`, is
evaluated. A returned manager (`Order.objects`: return `Order.objects.all()`), a
`RawQuerySet` and a queryset nested in a dict, a list or a schema are left to Ninja's
serialization in the event loop, where they raise `SynchronousOnlyOperation`: a 500
that reads no row (fail-closed). Evaluate those in the operation (`[o async for o in
qs]`, inside the context) before returning them.

### The user

The user (`get_rls_user()`) is:

- `request.auth` when the `auth=` callback returned a `TenantUser`;
- **nobody** when it returned anything else, such as an API key or token object:
  the operation runs with no RLS context, whatever context encloses the request (the
  one `RLSTenantMiddleware` set for a session cookie included), and `RLSTenantGuard`
  refuses the request with a 401. The same holds for an override that returns `None`
  (or any user who is not authenticated) for such a request. Override
  `get_rls_user()` to map such a result to its user:

    ```python
    class MyAPI(RLSTenantNinjaAPI):
        def get_rls_user(self, request):
            return request.auth.owner  # the user the API key belongs to
    ```

- `request.user` (`None` without `AuthenticationMiddleware`) only for an operation
  without `auth=`.

The default never falls back to the session's user once an `auth=` callback
authenticated the request: a cross-site request can carry an attacker's API key
alongside the victim's session cookie, and Ninja checks CSRF only for cookie
authentication (`APIKeyCookie`, `django_auth`). An operation without `auth=` is
served without a CSRF check too (Ninja's views are `csrf_exempt`), so its session
user is the cookie's, whoever sent the request; give such operations `auth=django_auth`
when they act for the session user.

`get_rls_user()` is synchronous; an `async def` operation calls it in a `sync_to_async`
hop. `RLSTenantGuard` checks the user it returned for the request, also when an
overridden `get_rls_context()` / `aget_rls_context()` enters nothing.

### Choosing the context

```python
class MyAPI(RLSTenantNinjaAPI):
    def get_rls_context(self, request):
        ...  # a context manager, or None to enter none

    async def aget_rls_context(self, request):
        ...  # an async context manager, or None
```

`get_rls_context()` serves `def` operations and `aget_rls_context()` serves
`async def` ones: **override both** to keep them in step. They run once per request,
after Ninja authenticated it. The defaults return a no-context block (sync or async)
when an `auth=` callback authenticated the request but `get_rls_user()` names no
user, and `None` for a request without `auth=` and without a session user and when the
active context is already the user's. An override decides for itself: when it
returns `None`, the operation runs in whatever context encloses the request, so call
`super()` for the requests it does not handle. An override is where a
selected tenant is validated: `RLSTenantGuard` accepts the tenant context it returns
for the request, whichever tenant that is (`admin_context()` only for a tenant admin).

### A user who belongs to several tenants

For a [`MultiTenantUser`](multi-tenant-users.md) who is not a tenant admin, the tenant
is the one the request selects: the `RLS_TENANTS["TENANT_HEADER"]` header, then the
session's `switch_tenant()` selection (only for the session's own user), then
`rls_tenant_id`. The selection must be one of the user's tenants; anything else is a
403 with the one not-selectable message (no existence oracle) and a
`tenant_not_selectable` audit record. When the tenant could depend on the header, the
response gets it in `Vary`. See [Users in Several Tenants](multi-tenant-users.md).

## `RLSTenantGuard`

Refuses a request served outside the context its user implies: an unauthenticated
request gets a 401, any other refusal a 403 (`RLSTenantGuard.message`). The operation
then answers 401/403 instead of an empty result, for example on a plain `NinjaAPI`
that a token client reached as an anonymous user of the middleware.

The rule is the one of `RLSTenantPermission` (#150): an RLS context must be active and,
for a user who is not a tenant admin, it must be a tenant context (never the admin
bypass) that is the user's own (`rls_tenant_id`), one of `rls_tenant_ids` for a
`MultiTenantUser`, or the one the API entered for this request (what
`get_rls_context()` returned, while it is the active one). A tenant admin passes in
any context. A refusal of an authenticated user is a `context.denied` audit record.

Two ways to apply it:

```python
api = RLSTenantNinjaAPI(rls_guard=RLSTenantGuard())  # every operation

guard = RLSTenantGuard()


@router.get("/orders/", response=list[OrderOut])
@guard  # one operation, under the route decorator
def list_orders(request):
    return Order.objects.all()
```

`rls_guard=` runs inside the context the API entered. `@guard` also works on a plain
`NinjaAPI`, where it checks the context the middleware set. `has_object_permission(request, obj)`
checks an object's tenant foreign key against the active tenant (always true in the
admin context); call it in the operation.

## Schemas and `rls_save()`

`RLSTenantModelSchema` is a `ModelSchema` without the model's tenant field, whatever
`Meta.fields` / `Meta.exclude` say: a payload never sets it and a response never shows
it. Declaring the field in the class body raises `RLSConfigurationError`. A model
without a tenant field is left alone. As with `ModelSchema`, only a direct subclass is
built from its `Meta`; a subclass of a built schema keeps its fields, and declaring
the tenant field in its body raises `RLSConfigurationError` too.

`rls_save(request, instance, payload, exclude_unset=False, loc=...)` writes the payload
to the instance and saves it:

- the primary key and the tenant field are never taken from the payload; in a tenant
  context the tenant is the active one, in the admin context the instance's own is
  kept (with neither, `NoTenantContextError`);
- every foreign key to an RLS-protected model is checked against the row's tenant
  (the [cross-tenant foreign key check](protecting-models.md#cross-tenant-foreign-keys));
  a failure is a Ninja `ValidationError`, answered with a 422;
- a missing target and another tenant's row give the **identical** 422, which echoes
  the value the payload sent, so the response is no existence oracle. A target that
  exists in another tenant is logged as `fk.cross_tenant_rejected`.

`exclude_unset=True` writes only the fields the client sent (a partial update). Use
`arls_save()` in an `async def` operation.

## Audit records

Records of the integration carry `source="ninja"`: `context.entered` / `context.exited`
for the context the API enters, `context.denied` from the guard and the 403s above
(`user_without_tenant`, `no_context`, `context_mismatch`, `object_of_other_tenant`,
`tenant_not_selectable`), and `fk.cross_tenant_rejected` from `rls_save()`. See
[Audit Logging](audit-logging.md).

## Limitations

- **Streaming.** A streaming operation (`response=JSONL[...]` or `SSE[...]`, a
  generator or async generator view) is consumed by Ninja after `run()` returns,
  outside any context the API could keep open, and after `RLSTenantMiddleware`'s has
  ended. The API passes such a view through without a context, so it reads zero rows
  unless the generator opens a context itself (`tenant_context()` around its body).
  Do not put a streaming operation under `RLSTenantGuard`: with no context it refuses
  a `def` generator view with a 403 before the response starts, and an `async`
  generator view only when the stream starts, after the response has begun, which the
  refusal ends with an error. Mount streaming operations on a router or an API
  without `rls_guard=`.
- **Decorators added later.** `api.add_decorator()` and `router.add_decorator()`
  wrap the library's decorators: they run before the context is entered and after it
  is exited, so they see no context.
- **Ninja's `add_decorator` modes.** Ninja's docstring has the names inverted:
  `mode="view"` wraps the operation's `run()` and `mode="operation"` wraps the view
  function. The API relies on the observed behaviour (the library's tests pin it), so
  a Ninja release that changes it fails those tests first.
- Only Ninja operations are covered. A plain Django view in the same project still
  needs `RLSTenantMiddleware` or an explicit context manager.
