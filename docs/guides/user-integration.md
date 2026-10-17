# User Integration

django-rls-tenants uses a **protocol** (structural typing) to interact with your user
model. This means your user model does not need to inherit from any specific base class --
it just needs to implement two properties.

## The TenantUser Protocol

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class TenantUser(Protocol):
    @property
    def is_tenant_admin(self) -> bool:
        """Return True if this user bypasses RLS (super-admin)."""
        ...

    @property
    def rls_tenant_id(self) -> int | str | None:
        """Return the tenant ID for RLS filtering, or None for admins."""
        ...
```

Any object that has these two properties satisfies the protocol. You do not need to
import `TenantUser` or register your model anywhere.

## Implementing on Your User Model

All three variants below keep `User` a plain `AbstractUser` with a nullable `tenant`
foreign key. The user model does not need to be RLS-protected for the protocol to work,
and the library's own example project and test app use exactly this shape. If you want
user rows isolated per tenant as well, read
[Protecting the User Model](protecting-models.md#protecting-the-user-model) first:
inheriting from `RLSProtectedModel` also puts RLS on Django's auth through tables.

### Simple Implementation

```python title="myapp/models.py"
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    tenant = models.ForeignKey(
        "myapp.Tenant",
        on_delete=models.CASCADE,
        null=True,    # null for admin users
        blank=True,
    )

    @property
    def is_tenant_admin(self) -> bool:
        return self.is_superuser

    @property
    def rls_tenant_id(self) -> int | None:
        return self.tenant_id if self.tenant_id else None
```

### Role-Based Implementation

```python title="myapp/models.py"
class User(AbstractUser):
    tenant = models.ForeignKey(
        "myapp.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    role = models.CharField(
        max_length=20,
        choices=[
            ("member", "Member"),
            ("manager", "Manager"),
            ("admin", "Admin"),
        ],
        default="member",
    )

    @property
    def is_tenant_admin(self) -> bool:
        # Only platform-level admins bypass RLS.
        # Tenant-level "admin" role still sees only their tenant's data.
        return self.is_superuser

    @property
    def rls_tenant_id(self) -> int | None:
        return self.tenant_id if self.tenant_id else None
```

### UUID Tenant Implementation

```python title="myapp/models.py"
import uuid

class User(AbstractUser):
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
    def rls_tenant_id(self) -> uuid.UUID | None:
        return self.tenant_id if self.tenant_id else None
```

## How the Protocol Is Used

The `TenantUser` protocol is used in three places:

### 1. Middleware

`RLSTenantMiddleware` reads `request.user` and calls `is_tenant_admin` and
`rls_tenant_id` to determine which GUC variables to set:

- **Admin users** (`is_tenant_admin=True`): sets `rls.is_admin = 'true'`
- **Tenant users**: sets `rls.current_tenant = str(rls_tenant_id)` and `rls.is_admin = 'false'`
- **Unauthenticated users**: no GUCs set (fail-closed)

A user who also implements `rls_tenant_ids` (below) may select another of their tenants
for a request; the middleware then uses the tenant they selected instead of
`rls_tenant_id`.

### 2. QuerySet.for_user()

```python
orders = Order.objects.for_user(request.user)
```

The `for_user()` method reads the same properties to apply both a Django ORM filter
(defense-in-depth) and the GUC variables. The GUCs are set only while the queryset runs
SQL, by every method that does: iteration, `count()`, `exists()`, `aggregate()`,
`iterator()`, `update()`, `delete()`, `create()`, the bulk methods and their async
variants. Afterwards the values the connection had before are restored, so a
`for_user()` query inside the middleware or a context manager leaves that context
intact. See [Lazy QuerySet Evaluation](../advanced/architecture.md#lazy-queryset-evaluation).

### 3. @with_rls_context Decorator

```python
@with_rls_context
def process_order(request, as_user):
    # RLS context set automatically from as_user
    ...
```

It works on `async def` functions too (in an async view, pass `await request.auser()`,
not `request.user`); see [Async Functions](context-managers.md#async-functions).

## Users in Several Tenants

*1.6.0+.* `TenantUser` gives a user one tenant. A user who belongs to several (a
consultant, a support agent) implements the `MultiTenantUser` protocol, which adds
`rls_tenant_ids`, the tenants they may select:

```python
from functools import cached_property


class User(AbstractUser):
    # ... tenant, is_tenant_admin and rls_tenant_id as above; rls_tenant_id
    # is the default tenant, used when a request selects none.

    @cached_property
    def rls_tenant_ids(self) -> frozenset[int]:
        return frozenset(self.memberships.values_list("tenant_id", flat=True))
```

A request then selects its tenant with a header (`X-Tenant`) or, in a browser, with
`switch_tenant()`; the library checks the choice against `rls_tenant_ids` and
`rls_tenant_id` and refuses any other with a 403. Users that implement only `TenantUser`
are unaffected. See [Users in Several Tenants](multi-tenant-users.md).

## Non-User Objects

The protocol is structural, so any object with the right properties works. This is
useful for service-layer functions or background tasks:

```python
from dataclasses import dataclass


@dataclass
class ServiceContext:
    """Lightweight context for background tasks."""
    tenant_id: int
    admin: bool = False

    @property
    def is_tenant_admin(self) -> bool:
        return self.admin

    @property
    def rls_tenant_id(self) -> int | None:
        return self.tenant_id if not self.admin else None


# Use in a Celery task:
def process_batch(tenant_id: int):
    ctx = ServiceContext(tenant_id=tenant_id)
    orders = Order.objects.for_user(ctx)
    if not orders.exists():
        return
    for order in orders.iterator():
        ...
    orders.filter(amount__lt=0).update(amount=0)
```

Each of these calls applies the context for its own SQL. Before 1.4.0 only iteration did:
`exists()`, `count()`, `iterator()`, `update()` and the other methods ran without the
GUCs, so outside a context they saw no rows (#131).

For a full Celery integration pattern using `tenant_context()` directly, see [Celery Tasks](celery-tasks.md).

## Runtime Type Checking

`TenantUser` is decorated with `@runtime_checkable`, so you can use `isinstance()`
checks at runtime:

```python
from django_rls_tenants import TenantUser

def set_context(user: object) -> None:
    if not isinstance(user, TenantUser):
        raise TypeError(f"{type(user).__name__} does not implement TenantUser protocol")
    # ...
```

`MultiTenantUser` is `@runtime_checkable` too (import it from `django_rls_tenants`).
`isinstance()` checks that the attributes exist and does not evaluate `rls_tenant_ids`.
