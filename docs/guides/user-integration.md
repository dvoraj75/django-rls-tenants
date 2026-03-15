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

### Simple Implementation

```python title="myapp/models.py"
from django.contrib.auth.models import AbstractUser
from django.db import models
from django_rls_tenants import RLSProtectedModel


class User(AbstractUser, RLSProtectedModel):
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
class User(AbstractUser, RLSProtectedModel):
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

class User(AbstractUser, RLSProtectedModel):
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

### 2. QuerySet.for_user()

```python
orders = Order.objects.for_user(request.user)
```

The `for_user()` method reads the same properties to apply both a Django ORM filter
(defense-in-depth) and set GUC variables at query evaluation time.

### 3. @with_rls_context Decorator

```python
@with_rls_context
def process_order(request, as_user):
    # RLS context set automatically from as_user
    ...
```

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
    for order in orders:
        ...
```

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
