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
- an unauthenticated request enters nothing: the operation runs in whatever context
  encloses it, none unless `RLSTenantMiddleware` set one for the session cookie.
  A request whose `auth=` callback fails gets Ninja's 401 before anything is entered.

The context stays active for the operation, the serialization of what it returns (a
lazy queryset included) and the rendering of an error, and is exited before the
response leaves the API, also when the operation raises. Input parsing runs before the
context is entered, so it runs outside it. The API covers every router mounted on it.

Both `def` and `async def` operations are supported. An `async def` operation gets
`async_admin_context()` / `async_tenant_context()`, entered in the request's
coroutine; the user lookup and the session read run in one thread-sensitive
`sync_to_async` hop. Under ASGI, Ninja runs a `def` operation in a thread, and the API
enters the synchronous context there.

### The user

The user is `request.auth` when the `auth=` callback returned a `TenantUser`, else
`request.user` (`None` without `AuthenticationMiddleware`). Override `get_rls_user()`
to choose otherwise. It is synchronous; an `async def` operation calls it in a
`sync_to_async` hop.

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
after Ninja authenticated it. The defaults return `None` for an unauthenticated
request and when the active context is already the user's. An override is where a
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
built from its `Meta`.

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
  outside any context the API could keep open. The API passes such a view through
  without a context, so it reads zero rows unless an enclosing context
  (`RLSTenantMiddleware` for a session user) covers the streaming. With the guard, a
  `def` generator view is checked when the view is called, before the generator
  runs; an `async` generator is checked when the response starts streaming, so a
  refusal there ends the stream with an error.
- **Decorators added later.** `api.add_decorator()` and `router.add_decorator()`
  wrap the library's decorators: they run before the context is entered and after it
  is exited, so they see no context.
- **Ninja's `add_decorator` modes.** Ninja's docstring has the names inverted:
  `mode="view"` wraps the operation's `run()` and `mode="operation"` wraps the view
  function. The API relies on the observed behaviour (the library's tests pin it), so
  a Ninja release that changes it fails those tests first.
- Only Ninja operations are covered. A plain Django view in the same project still
  needs `RLSTenantMiddleware` or an explicit context manager.
