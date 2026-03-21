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
    CASE WHEN current_setting('rls.is_admin', true) = 'true'
         THEN true
         ELSE tenant_id = nullif(
             current_setting('rls.current_tenant', true), '')::int
    END
)
WITH CHECK (
    CASE WHEN current_setting('rls.is_admin', true) = 'true'
         THEN true
         ELSE tenant_id = nullif(
             current_setting('rls.current_tenant', true), '')::int
    END
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

## Automatic Query Scoping

When a tenant context is active (via `tenant_context()`, `admin_context()`, or
`RLSTenantMiddleware`), `RLSManager` **automatically** adds `WHERE tenant_id = X`
to every query. No extra calls are needed:

```python
from django_rls_tenants import tenant_context, admin_context

# Queries are automatically scoped -- no for_user() needed
with tenant_context(tenant_id=42):
    orders = Order.objects.all()           # WHERE tenant_id = 42
    active = Order.objects.filter(active=True)  # WHERE tenant_id = 42 AND active

# Admin context -- no filter (sees all rows)
with admin_context():
    all_orders = Order.objects.all()       # no tenant filter
```

In views with `RLSTenantMiddleware`, this happens transparently:

```python
def list_orders(request):
    # Middleware already set the context -- queries are auto-scoped
    orders = Order.objects.filter(is_active=True)
    return render(request, "orders/list.html", {"orders": orders})
```

**Why this matters:** PostgreSQL's `current_setting()` function used in RLS policies
is not leakproof, so the planner cannot push the RLS predicate into index scans.
The automatic ORM-level `WHERE tenant_id = X` filter enables composite indexes,
eliminating sequential scan penalties at scale.

## Strict Mode Guard

When `STRICT_MODE=True` in your `RLS_TENANTS` configuration, `TenantQuerySet`
evaluation methods raise `NoTenantContextError` if no RLS context is active.
This catches accidental unscoped queries at the point of execution:

```python
from django_rls_tenants.exceptions import NoTenantContextError

# Without context -- raises in strict mode
Order.objects.count()       # NoTenantContextError
Order.objects.all().first() # NoTenantContextError
Order.objects.filter(active=True).exists()  # NoTenantContextError

# With context -- works normally
with tenant_context(tenant_id=42):
    Order.objects.count()   # OK
```

The following queryset methods are guarded: iteration (`_fetch_all()`), `count()`,
`exists()`, `aggregate()`, `update()`, `delete()`, `iterator()`, `bulk_create()`,
`bulk_update()`, `get()`, `first()`, `last()`.

Queryset *construction* (e.g., `Order.objects.filter(...)`) does not trigger the
check -- only evaluation does. This matches Django's lazy queryset philosophy.

See [Configuration](../getting-started/configuration.md#strict_mode) for setup.

## Using for_user()

`for_user()` is still available and works as before. It scopes queries to a specific
user's tenant and sets GUC variables at query evaluation time:

```python
# Explicit scoping (still works, useful outside context managers)
def list_orders(request):
    orders = Order.objects.for_user(request.user)
    return render(request, "orders/list.html", {"orders": orders})
```

If both auto-scoping and `for_user()` are active simultaneously, the query gets two
redundant `WHERE tenant_id = X` clauses. This is by design for defense-in-depth; the
cost of the double equality check per row is negligible.

## Querying with Context Managers

Context managers set the RLS context for any code block:

```python
from django_rls_tenants import tenant_context, admin_context

# As a specific tenant (queries auto-scoped)
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

## Many-to-Many Relationships

M2M relationships between `RLSProtectedModel` subclasses are automatically detected
and protected. When Django auto-generates a through table for an M2M field,
`django-rls-tenants` registers an `RLSM2MConstraint` on it during `AppConfig.ready()`.

### Automatic Protection

Simply define your M2M fields as usual:

```python
from django_rls_tenants import RLSProtectedModel

class Tag(models.Model):
    """Non-RLS model (shared across tenants)."""
    name = models.CharField(max_length=100)

class Project(RLSProtectedModel):
    name = models.CharField(max_length=100)
    members = models.ManyToManyField("User")   # both sides RLS-protected
    tags = models.ManyToManyField(Tag)          # one side RLS-protected

    class Meta(RLSProtectedModel.Meta):
        db_table = "myapp_project"
```

The auto-generated through tables (`myapp_project_members`, `myapp_project_tags`)
will get `EXISTS`-based subquery RLS policies that check each FK reference belongs
to the current tenant.

### Migration Operation

For explicit control in migrations, use `AddM2MRLSPolicy`:

```python
from django.db import migrations
import django_rls_tenants.operations

class Migration(migrations.Migration):
    operations = [
        django_rls_tenants.operations.AddM2MRLSPolicy(
            m2m_table="myapp_project_members",
            from_model="myapp.Project",
            to_model="myapp.User",
            from_fk="project_id",
            to_fk="user_id",
            from_tenant_fk="tenant",  # or None if not RLS-protected
            to_tenant_fk="tenant",
        ),
    ]
```

This operation is **reversible** -- rolling back the migration drops the policy and
disables RLS on the through table.

### Supported Scenarios

| Scenario | Example | Policy checks |
|----------|---------|---------------|
| Both sides protected | `Project.members` (Project + User) | Both FK subqueries |
| One side protected | `Project.tags` (Project + Tag) | Only the protected FK |
| Self-referential | `SelfRefModel.friends` | Both FKs against same table |

### Explicit Through Models

If you define an explicit through model (e.g., with extra fields), make it a
`RLSProtectedModel` subclass and manage RLS yourself -- the auto-detection skips
explicit through models.

```python
class ProjectMembership(RLSProtectedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=50)

    class Meta(RLSProtectedModel.Meta):
        db_table = "myapp_project_membership"

class Project(RLSProtectedModel):
    members = models.ManyToManyField("User", through=ProjectMembership)
```

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
