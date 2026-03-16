# Quick Start

This tutorial walks you through setting up django-rls-tenants from scratch. By the end
you will have a working multitenant application with database-enforced row isolation.

## 1. Define Your Tenant Model

Create a model to represent tenants. Any model with an integer or UUID primary key works:

```python title="myapp/models.py"
from django.db import models


class Tenant(models.Model):
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name
```

## 2. Protect Models with RLS

Inherit from `RLSProtectedModel` to add automatic tenant isolation:

```python title="myapp/models.py"
from django_rls_tenants import RLSProtectedModel


class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self) -> str:
        return self.title
```

`RLSProtectedModel` automatically:

- Adds a `tenant` ForeignKey pointing to your tenant model.
- Sets `RLSManager` as the default manager, which automatically scopes queries
  to the current tenant when a context is active (via middleware or `tenant_context()`).
  Also provides `for_user()` for explicit scoping.
- Includes an `RLSConstraint` that generates the RLS policy during migrations.

## 3. Implement the TenantUser Protocol

Your User model must expose two properties so the library knows which tenant a user
belongs to and whether they are an admin:

```python title="myapp/models.py"
from django.contrib.auth.models import AbstractUser


class User(AbstractUser, RLSProtectedModel):
    # Override the auto-generated tenant FK to allow null (for admins)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    @property
    def is_tenant_admin(self) -> bool:
        """Admins bypass RLS and see all tenants."""
        return self.is_superuser

    @property
    def rls_tenant_id(self) -> int | None:
        """Return the tenant PK for RLS filtering."""
        return self.tenant_id if self.tenant_id else None
```

See [User Integration](../guides/user-integration.md) for details on the `TenantUser` protocol.

## 4. Configure Settings

```python title="settings.py"
RLS_TENANTS = {
    # Required: dotted path to your tenant model
    "TENANT_MODEL": "myapp.Tenant",

    # Optional (shown with defaults):
    "TENANT_FK_FIELD": "tenant",       # FK field name on protected models
    "GUC_PREFIX": "rls",               # PostgreSQL GUC variable prefix
    "USER_PARAM_NAME": "as_user",      # Parameter name for @with_rls_context
    "TENANT_PK_TYPE": "int",           # SQL cast type: "int", "bigint", or "uuid"
    "USE_LOCAL_SET": False,            # Use SET LOCAL (for connection pooling)
}
```

See [Configuration](configuration.md) for a detailed explanation of each setting.

## 5. Add Middleware

```python title="settings.py"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Add after AuthenticationMiddleware:
    "django_rls_tenants.RLSTenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
```

!!! important
    `RLSTenantMiddleware` must come **after** `AuthenticationMiddleware` because it
    reads `request.user` to determine the tenant context.

The middleware automatically scopes all queries on RLS-protected models. In your
views, `Order.objects.all()` returns only the current tenant's rows -- no
`for_user()` call needed. The database-level RLS policy acts as a safety net on
top of the ORM-level filter.

## 6. Run Migrations

```bash
python manage.py makemigrations
python manage.py migrate
```

The migration will:

1. Create your tables as usual.
2. Execute `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`.
3. Create a `CREATE POLICY` statement with tenant isolation rules.

## 7. Verify RLS Policies

```bash
python manage.py check_rls
```

Expected output:

```
  Order (myapp_order): myapp_order_tenant_isolation_policy
  User (myapp_user): myapp_user_tenant_isolation_policy

All 2 RLS-protected tables verified.
```

If any policies are missing, the command exits with a non-zero status and lists the issues.

## What's Next?

- [Configuration](configuration.md) -- understand all 6 settings.
- [Protecting Models](../guides/protecting-models.md) -- customize FK fields and constraints.
- [Context Managers](../guides/context-managers.md) -- use `tenant_context` and `admin_context` in scripts.
- [Testing](../guides/testing.md) -- test helpers for RLS verification.
