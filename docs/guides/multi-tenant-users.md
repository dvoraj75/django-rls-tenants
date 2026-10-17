# Users in Several Tenants

*1.6.0+.* The [`TenantUser`](user-integration.md) protocol gives a user one tenant,
`rls_tenant_id`. Some users belong to several: a consultant who works for two customers,
a support agent, an account that is a member of more than one organization. For them the
library has an opt-in protocol, `MultiTenantUser`, and a way to **select** the tenant a
request runs in: a request header for API clients, a session selection for browsers.

Nothing changes for a user who implements only `TenantUser`.

## The `MultiTenantUser` protocol

`MultiTenantUser` extends `TenantUser` with one property, `rls_tenant_ids`: the tenants
the user may select.

```python
from collections.abc import Collection
from typing import Protocol, runtime_checkable


@runtime_checkable
class MultiTenantUser(TenantUser, Protocol):
    @property
    def rls_tenant_ids(self) -> Collection[int | str]:
        """Return the IDs of the tenants this user may select."""
        ...
```

`rls_tenant_id` keeps its meaning: the **default** tenant, the one a request runs in when
it selects none. It may be `None` (the user has no default). The default is always
selectable, even when it is missing from `rls_tenant_ids`.

As with `TenantUser`, you do not inherit from it or register anything; a user model that
has the property satisfies the protocol.

### Implementing it

Keep the memberships in a plain model and read them from the user:

```python title="myapp/models.py"
from functools import cached_property

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    # The default tenant: where a request runs when it selects none.
    tenant = models.ForeignKey(
        "myapp.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    @property
    def is_tenant_admin(self) -> bool:
        return self.is_superuser

    @property
    def rls_tenant_id(self) -> int | None:
        return self.tenant_id

    @cached_property
    def rls_tenant_ids(self) -> frozenset[int]:
        return frozenset(self.memberships.values_list("tenant_id", flat=True))


class Membership(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey("myapp.Tenant", on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="unique_membership"),
        ]
```

Three rules for `rls_tenant_ids`:

- **Cache it** (`functools.cached_property`). It is read only when a request selects a
  tenant, by `RLSTenantPermission` and by the admin's tenant switcher, but possibly
  several times in one request. A property that runs a query each time costs a query each
  time.
- **Keep the membership table plain**, not RLS-protected. The library reads it to decide
  which tenant a request runs in, before any RLS context exists. If you protect it
  anyway, read it inside `admin_context()` in `rls_tenant_ids`.
- **Return the primary keys of the tenant model** (`int`, `UUID` or their string forms).
  The library compares them with the selection after converting both sides with the tenant
  model's primary key field, so `"2"` matches `2` and a UUID string matches a `UUID`.

`isinstance(user, MultiTenantUser)` does not evaluate the property (it only checks that
the attribute exists), so the check itself runs no query.

A tenant admin (`is_tenant_admin=True`) never selects a tenant, whatever `rls_tenant_ids`
says: it runs in `admin_context()`, as before, and the header is ignored for it.

## How a request picks its tenant

For a `MultiTenantUser` who is not a tenant admin, the library takes the first of:

| Order | Source | Set by | Lasts |
|-------|--------|--------|-------|
| 1 | The request header `RLS_TENANTS["TENANT_HEADER"]` (default `X-Tenant`) | An API client | That request |
| 2 | The session key `RLS_TENANTS["TENANT_SESSION_KEY"]` (default `rls_tenant`) | `switch_tenant()` | Until `clear_tenant_selection()`, another `switch_tenant()` or the end of the session |
| 3 | `rls_tenant_id` | The user model | |

A header wins over the session. Either source is checked against the user's tenants
(`rls_tenant_id` and `rls_tenant_ids`) on **every request**, so a membership you revoke
takes effect on the user's next request. Set a setting to `None` to turn its source off;
see [`TENANT_HEADER`](../getting-started/configuration.md#tenant_header) and
[`TENANT_SESSION_KEY`](../getting-started/configuration.md#tenant_session_key). The header
name is case-insensitive. The session source needs `SessionMiddleware`; without a session
only the header works.

Three integrations resolve the tenant this way:

| Integration | Behaviour |
|-------------|-----------|
| [`RLSTenantMiddleware`](middleware.md#tenant-selection) | Sets the selected tenant's context for the request (WSGI, ASGI and subclasses alike) |
| [`RLSTenantAPIViewMixin`](drf.md#a-user-who-belongs-to-several-tenants) | The default `get_rls_context()` enters the selected tenant for token clients |
| [`RLSTenantModelAdmin`](admin.md#users-in-several-tenants) | Scopes the admin to it, with a tenant switcher |

## Switching tenants from a view

`switch_tenant(request, tenant_id)` validates the choice against `request.user` and stores
it in the session. Call it from a view that handles the user's choice, then redirect:

```python title="views.py"
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from django_rls_tenants import clear_tenant_selection, switch_tenant


@login_required
@require_POST
def select_tenant(request):
    tenant_id = request.POST.get("tenant", "")
    if tenant_id:
        switch_tenant(request, tenant_id)     # 403 unless it is one of the user's
    else:
        clear_tenant_selection(request)       # back to rls_tenant_id
    return redirect("dashboard")
```

```html title="base.html"
<form method="post" action="{% url 'select_tenant' %}">
  {% csrf_token %}
  <select name="tenant" onchange="this.form.submit()">
    {% for tenant in selectable_tenants %}
      <option value="{{ tenant.pk }}"{% if tenant == current_tenant %} selected{% endif %}>
        {{ tenant.name }}
      </option>
    {% endfor %}
  </select>
</form>
```

`selectable_tenants` and `current_tenant` come from your own code (a context processor, for
instance); the [example project](#example-project) has one.

- **It takes effect from the next request.** The request that calls `switch_tenant()`
  keeps the context it started in, which is why the view redirects.
- **Use a `POST` view.** Django's CSRF protection then covers the switch (see
  [Security](#security)).
- `switch_tenant()` returns the tenant's primary key, converted to the tenant model's type.
- It raises `django.core.exceptions.PermissionDenied` (a 403) if the user is anonymous, a
  tenant admin, not a `MultiTenantUser`, not the user the session is logged in as (a
  request DRF authenticated with a token: such a client selects with the header), or may not
  select that tenant. The session is then left as it was.
- It raises `RLSConfigurationError` if `TENANT_SESSION_KEY` is `None` or the request has no
  session.
- `logout()` flushes the session, which ends the selection.
- `clear_tenant_selection(request)` removes the selection; it raises
  `RLSConfigurationError` in the same two cases and does nothing if there was none. It does
  nothing either unless the session is logged in as `request.user`.

Import both from `django_rls_tenants` (or `django_rls_tenants.tenants`).

Inside a request, query with the plain manager: the middleware's context already is the
selected tenant, so `Order.objects.all()` returns its rows. `for_user(request.user)` would
use the user's default tenant instead (see [What does not change](#what-does-not-change)).

## API clients

A client sends the header with every request:

```bash
curl -H "Authorization: Token <key>" -H "X-Tenant: 7" https://example.com/api/orders/
```

With [`RLSTenantAPIViewMixin`](drf.md) the default `get_rls_context()` reads it after DRF
has authenticated the user. A session-authenticated API client may use the session
selection instead: the middleware has already set that context, and the mixin enters
nothing. The session selection belongs to the user the session is logged in as: a request
that carries one user's session cookie and another user's token neither uses nor drops it.

A response whose tenant could depend on the header (that of a `MultiTenantUser`'s request,
while `TENANT_HEADER` is set) carries it in `Vary`, which the middleware and the DRF mixin
add, so a cache does not serve the response for one tenant to a request for another.

## Refusals, stale selections and no default

| Situation | Result |
|-----------|--------|
| The header names a tenant the user may not select, a malformed value (`abc`), an empty value, or a tenant that does not exist | 403. The session is untouched |
| The session selection is no longer one of the user's (membership revoked) | 403, and the selection is **removed**: the next request runs in `rls_tenant_id` |
| No selection, and `rls_tenant_id` is set | The default tenant |
| No selection and no `rls_tenant_id` | See below |

Every refusal carries the same message, **"The selected tenant is not one you can
access."**, whether the tenant exists or not, so the response is not an existence oracle.
A refusal is never a silent fallback to another tenant for that request. Under DRF it is a
`403` with `{"detail": "The selected tenant is not one you can access."}`.

A member with **no selection and no default** gets no context from the middleware: it
treats the request like an anonymous one (fail-closed). Protected tables read nothing, and
with `STRICT_MODE` a query raises `NoTenantContextError`. This is deliberately not a 403,
so that a tenant-picker page, which reads only plain tables such as the memberships and
the tenant model, can render. The DRF mixin answers such a request `403` ("Your account
is not assigned to a tenant."), and the admin raises `PermissionDenied` while
`rls_deny_without_tenant` is `True`.

A plain `TenantUser` with `rls_tenant_id=None` still raises `NoTenantContextError`, as
before.

## What does not change

- **`for_user()` and `@with_rls_context`** use `rls_tenant_id`, the default tenant. They
  know the user, not the request, and do not read the header or the session. For a member
  with no default they raise `NoTenantContextError`, as for any tenantless user. In a
  request, use the plain manager (above); outside one, pass the tenant explicitly with
  `tenant_context()`.
- **Celery.** A task enqueued by a request carries the request's **effective** context, so
  a request that selected tenant B enqueues a task that runs in B. See
  [Celery Tasks](celery-tasks.md).
- **Management commands** take their tenant from `--tenant`, as before.
- **Tenant admins** run in `admin_context()` and ignore the header.

## Audit records

With [audit logging](audit-logging.md) enabled, a switch logs an `INFO` record:

```
tenant.switched source=selection tenant_id=7 admin=False user_id=3 previous=5
```

`rls_tenant_id` is the new selection (`None` when it was cleared) and `rls_previous` the
value the session held before (a string), or `None`. `clear_tenant_selection()` logs
nothing when there was no selection. A refused selection logs a `context.denied` record
(`WARNING`) with the reason `tenant_not_selectable`:

```
context.denied source=middleware user_id=3 reason=tenant_not_selectable selected=99 via=header
```

`rls_selected` is the raw value the client sent and `rls_via` says where it came from
(`header`, `session`, `query` for the admin switcher's query parameter, or `switch` for a
`switch_tenant()` call). The `context.entered` record of a request carries the tenant it
selected.

## Security

- **Every selection is checked** against the user's own tenants, in the same request that
  uses it. The worst a forged or stale selection can do is switch a user to another
  tenant they already belong to; it never grants access to a tenant they do not.
- **The header is safe against cross-site requests.** A cross-site form or image request
  cannot set a custom header; only same-origin JavaScript or a CORS-allowed origin can.
- **The session selection is a state change**, so set it from a CSRF-protected `POST`
  view. A cross-site `GET` that switched tenants would let an attacker change which tenant
  a victim works in. The admin's tenant switcher is a `GET` link, so it applies a
  member's choice only to a navigation from this site (see
  [Admin](admin.md#users-in-several-tenants)).
- **A long value is cut** to 64 characters in the audit record of a refusal.
- **Refusals do not distinguish** "not yours" from "does not exist" or "malformed".
- **The membership table is read before any context exists.** Keep it a plain table, or
  read it in `admin_context()` (see [Implementing it](#implementing-it)).

See also [Security Model](../advanced/security.md#users-in-several-tenants).

## Example project

The [example project](https://github.com/dvoraj75/django-rls-tenants/tree/main/example)
has a consultant, `dave` / `demo1234`, whose default tenant is Acme Corp and who is also a
member of Globex Inc (`accounts.Membership`, plain table). The header shows a tenant
switcher (`notes/context_processors.py`, `notes.views.select_tenant`), and the notes API
accepts `X-Tenant`. See the example's README for the steps.
