# Protecting Models

`RLSProtectedModel` is the abstract base class that adds database-enforced tenant
isolation to your models.

## Basic Usage

Inherit from `RLSProtectedModel` to protect a model:

```python title="myapp/models.py"
from django.db import models
from django_rls_tenants import RLSProtectedModel


class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
```

This automatically provides:

1. **A `tenant` ForeignKey** -- added dynamically via the `class_prepared` signal, pointing to your `TENANT_MODEL`.
2. **`RLSManager`** as the default manager -- with `for_user()` for scoped queries.
3. **`RLSConstraint`** in `Meta.constraints` -- generates the RLS policy during migrations.

## What the Migration Creates

When you run `makemigrations` and `migrate`, the `RLSConstraint` generates:

```sql
-- Enable RLS on the table
ALTER TABLE "myapp_order" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "myapp_order" FORCE ROW LEVEL SECURITY;

-- Create the isolation policy
CREATE POLICY "myapp_order_tenant_isolation_policy"
ON "myapp_order"
USING (
    tenant_id = coalesce(
        nullif(current_setting('rls.current_tenant', true), '')::int, NULL
    )
    OR coalesce(current_setting('rls.is_admin', true) = 'true', false)
)
WITH CHECK (
    tenant_id = coalesce(
        nullif(current_setting('rls.current_tenant', true), '')::int, NULL
    )
    OR coalesce(current_setting('rls.is_admin', true) = 'true', false)
);
```

The `USING` clause filters `SELECT`, `UPDATE`, and `DELETE`. The `WITH CHECK` clause
validates `INSERT` and `UPDATE` operations.

## Custom ForeignKey

If you need to customize the tenant FK (e.g., nullable for admin users, custom
`on_delete`, or a different related name), declare it directly on your model.
The `class_prepared` handler detects the existing field and skips the auto-generation:

```python
from django.db import models
from django_rls_tenants import RLSProtectedModel


class User(AbstractUser, RLSProtectedModel):
    # Custom FK: nullable for admins, custom related_name
    tenant = models.ForeignKey(
        "myapp.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="users",
    )

    @property
    def is_tenant_admin(self) -> bool:
        return self.is_superuser

    @property
    def rls_tenant_id(self) -> int | None:
        return self.tenant_id if self.tenant_id else None
```

!!! important
    The field **must be named `tenant`** (or whatever `TENANT_FK_FIELD` is set to)
    for the auto-detection to work. If you name it differently, the handler will add
    a duplicate FK.

## Using for_user()

The `RLSManager` provides `for_user()` to scope queries to a specific user's tenant:

```python
# In a view or service function:
def list_orders(request):
    orders = Order.objects.for_user(request.user)
    return render(request, "orders/list.html", {"orders": orders})
```

For admin users, `for_user()` returns all rows (RLS admin bypass is set at evaluation
time). For tenant users, it applies both a Django ORM filter (defense-in-depth) and
sets the GUC variable at query evaluation time.

## Querying with Context Managers

Alternatively, use context managers to set the RLS context for any code block:

```python
from django_rls_tenants import tenant_context, admin_context

# As a specific tenant
with tenant_context(tenant_id=42):
    orders = Order.objects.all()  # only tenant 42's orders

# As admin (see all)
with admin_context():
    all_orders = Order.objects.all()  # all tenants
```

## Meta Class Inheritance

If you define a custom `Meta` class, inherit from `RLSProtectedModel.Meta` to
preserve the constraint:

```python
class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "orders"
        ordering = ["-created_at"]
```

If you do not inherit from `RLSProtectedModel.Meta`, the `RLSConstraint` will not
be included and RLS will not be applied to the table.

## Extra Bypass Flags

For edge cases (e.g., authentication middleware that needs to read user records before
the tenant context is set), you can add custom bypass flags to the RLS policy:

```python
from django_rls_tenants.rls import RLSConstraint


class User(RLSProtectedModel):
    # ...

    class Meta(RLSProtectedModel.Meta):
        constraints = [
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
                extra_bypass_flags=["rls.auth_bypass"],
            ),
        ]
```

Extra bypass flags are added to the `USING` clause only (not `WITH CHECK`), so they
allow reading but not writing without proper tenant context.

See [Bypass Mode](bypass-mode.md) for more details.

## Multiple Protected Models

You can have as many `RLSProtectedModel` subclasses as needed. Each gets its own
RLS policy:

```python
class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)

class Invoice(RLSProtectedModel):
    number = models.CharField(max_length=50)

class Document(RLSProtectedModel):
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to="documents/")
```
