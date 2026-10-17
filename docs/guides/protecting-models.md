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
    CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
         THEN true
         ELSE tenant_id = nullif(
             (SELECT current_setting('rls.current_tenant', true)), '')::int
    END
)
WITH CHECK (
    CASE WHEN (SELECT current_setting('rls.is_admin', true)) = 'true'
         THEN true
         ELSE tenant_id = nullif(
             (SELECT current_setting('rls.current_tenant', true)), '')::int
    END
);
```

The `USING` clause filters `SELECT`, `UPDATE`, and `DELETE`. The `WITH CHECK` clause
validates `INSERT` and `UPDATE` operations.

## Custom ForeignKey

If you need to customize the tenant FK (e.g., nullable for admin users, custom
`on_delete`, or a different related name), declare it directly on your model.
The `class_prepared` handler detects the existing field and skips the auto-generation.
This example uses an RLS-protected `User`, the opt-in case described in
[Protecting the User Model](#protecting-the-user-model):

```python
from django.contrib.auth.models import AbstractUser
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

    class Meta(AbstractUser.Meta, RLSProtectedModel.Meta):
        pass

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

**Why this matters:** the RLS policy is a `CASE WHEN <admin> THEN true ELSE
tenant_id = ... END` expression, which PostgreSQL can only apply as a post-scan
filter, never as an index condition. The automatic ORM-level `WHERE tenant_id = X`
filter is a plain indexable clause, so it is what lets the planner use the tenant
FK index and any composite index you add. See the
[Performance & Indexing guide](performance.md) for the measured plans.

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
`exists()`, `aggregate()`, `update()`, `delete()`, `iterator()`, `aiterator()`,
`bulk_create()`, `bulk_update()`, `get()`, `first()`, `last()`.

Queryset *construction* (e.g., `Order.objects.filter(...)`) does not trigger the
check -- only evaluation does. This matches Django's lazy queryset philosophy. Reading a
queryset whose results are already cached runs no query and is not checked either.

See [Configuration](../getting-started/configuration.md#strict_mode) for setup.

## Using for_user()

`for_user()` is still available. It scopes queries to a specific user's tenant and sets
the GUC variables only while each query runs, restoring the previous values afterwards
(see [Lazy QuerySet Evaluation](../advanced/architecture.md#lazy-queryset-evaluation)):

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
be included and RLS will not be applied to the table. A `Meta` that inherits it but sets
`constraints = [...]` of its own has the same effect, because that list replaces the
inherited one: write `constraints = [*RLSProtectedModel.Meta.constraints, ...]`.

System check `django_rls_tenants.W014` (added in 1.5.0) reports each concrete model that
inherits `RLSProtectedModel` and has no `RLSConstraint` in `Meta.constraints`, so
`manage.py check` catches it before `makemigrations` writes a migration without a policy.
See [W014](../troubleshooting.md#w014-a-protected-model-has-no-rlsconstraint).

## Protecting the User Model

The [Quick Start](../getting-started/quickstart.md) and the
[User Integration](user-integration.md) guide keep `User` a plain `AbstractUser`. The
`TenantUser` protocol only needs the two properties, and Django reads user rows itself
during login and session loading before the request's tenant context is established.

Inheriting from `RLSProtectedModel` as well, `class User(AbstractUser, RLSProtectedModel)`,
requires explicit `Meta` inheritance, as in the [Custom ForeignKey](#custom-foreignkey)
example above. Without `class Meta(AbstractUser.Meta, RLSProtectedModel.Meta)`, Django
inherits `AbstractUser.Meta` alone, so the user table has no `RLSConstraint` and remains
unprotected even though the auth through tables are registered for RLS. The model keeps
Django's `UserManager`; its user queries rely on database RLS rather than the automatic
ORM scoping provided by `RLSManager`.

This is an opt-in with consequences beyond the `User` table:

!!! warning "An RLS-protected `User` also protects Django's auth through tables"
    `AbstractUser` declares the `groups` and `user_permissions` many-to-many fields.
    Their auto-generated through tables, `<app>_user_groups` and
    `<app>_user_user_permissions`, are M2M relations on an RLS-protected model, so
    `register_m2m_rls()` adds an `RLSM2MConstraint` to both at startup (see
    [Many-to-Many Relationships](#many-to-many-relationships)) and `check_rls` expects a
    policy on each of them. It lists them under an extra `M2M through tables:` section,
    counts them in the total (the Quick Start project reports
    `All 4 RLS-protected tables verified.` instead of `All 1 ...`), and exits non-zero
    until you apply the policies with an `AddM2MRLSPolicy` migration (`check_rls
    --generate` writes it) or `setup_m2m_rls`.

What this means in practice:

1. **Reads of `User` before the request context is set need a bypass.** Evaluating
   Django's lazy `request.user` calls the auth backend before `RLSTenantMiddleware` can
   establish the tenant context. Add `extra_bypass_flags` to the `RLSConstraint` (next
   section) and set the flag in both `authenticate()` and `get_user()`, as shown in
   [Use Case: Authentication](bypass-mode.md#use-case-authentication). Auth writes such
   as `last_login` updates also need a tenant or admin context; a read bypass does not
   allow them. With `STRICT_MODE=True`, also list the user model in
   [`STRICT_MODE_EXCLUDE`](../getting-started/configuration.md#strict_mode_exclude).
2. **Group and permission lookups need a tenant or admin context.** `RLSM2MConstraint`
   has no `extra_bypass_flags` option; its policy only passes for the admin GUC or when
   the referenced user belongs to the current tenant. Fresh database queries for group
   membership and direct permissions return no rows without that context. Inside an
   authenticated request the middleware provides one; in scripts, shells and Celery
   tasks evaluate the queries inside `tenant_context()` or `admin_context()`. Users
   with `tenant=None` need an admin context to read those relations.
3. **Permission results can outlive the RLS context.** Django's `ModelBackend` caches
   permissions on the user instance. A lookup before entering a context can cache an
   empty result, and a result loaded inside a context can remain available after it
   exits. Use a fresh user instance when changing contexts. Also, active Django
   superusers pass `has_perm()` without querying these tables, and custom backends may
   grant permissions independently of them.

The library's `example/` user and `tests/test_app.AdminUser` keep a plain `AbstractUser`.
The test app's separate `ProtectedUser` exercises extra bypass flags on an ordinary
RLS-protected model; it does not inherit Django's auth fields.

## Extra Bypass Flags

For edge cases (e.g., authentication middleware that needs to read user records before
the tenant context is set), you can add custom bypass flags to the RLS policy:

```python
from django_rls_tenants.rls import RLSConstraint


class User(AbstractUser, RLSProtectedModel):
    # Keep the nullable tenant FK and protocol properties from Custom ForeignKey.

    class Meta(AbstractUser.Meta, RLSProtectedModel.Meta):
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

M2M relationships between `RLSProtectedModel` subclasses are automatically detected.
When Django auto-generates a through table for an M2M field, `django-rls-tenants`
registers an `RLSM2MConstraint` on it during `AppConfig.ready()`. The policy itself still
has to be created: see [Automatic Protection](#automatic-protection) and
[Migration Operation](#migration-operation).

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
get an `RLSM2MConstraint` at startup. It describes the `EXISTS`-based subquery policy
that checks each FK reference belongs to the current tenant, but it lives on the runtime
through model only, outside the migration state, so **`migrate` does not create the
policy**. In a project with migrations, create it with an `AddM2MRLSPolicy` migration
(next section); `python manage.py check_rls --generate` writes it for you. For a single
database, or an app without migrations, `setup_m2m_rls` applies the policies directly.
`check_rls` reports a through table with no policy.

### Migration Operation

`AddM2MRLSPolicy` creates the policy in a migration. `python manage.py check_rls
--generate` (added in 1.5.0) writes one migration per app with an operation for every
through table that no migration covers, which you review and `migrate`; see
[Generating M2M policy migrations](management-commands.md#generating-m2m-policy-migrations).
To write it by hand:

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

## Cross-Tenant Foreign Keys

RLS does not stop a row of tenant A from holding a foreign key to a row of tenant B.
The policy checks the row's own tenant column, and PostgreSQL runs foreign key checks
outside row security (see [Security](../advanced/security.md#referential-integrity-and-unique-indexes)).
`RLSProtectedModel` therefore validates, in `clean_fields()`, that each foreign key to
another RLS-protected model points at a row of the **same tenant as the row itself**:

```python
class Order(RLSProtectedModel):
    customer = models.ForeignKey("Customer", on_delete=models.PROTECT)  # Customer is RLS-protected

order = Order(tenant=tenant_a, customer=customer_of_b)
order.full_clean()
# ValidationError: {'customer': ['customer instance with id 7 is not a valid choice.']}
```

This runs wherever Django calls `full_clean()`: a `ModelForm` (the Django admin
included) and an explicit `full_clean()`. A foreign key the form does not show is
skipped, as Django skips excluded fields. It does **not** run on `save()`,
`objects.create()`, `bulk_create()`, `update()`, `bulk_update()` or raw SQL. In service
code that writes with `save()` and no form, call the helper yourself:

```python
from django_rls_tenants import validate_cross_tenant_fks

order = Order(tenant=tenant, customer=customer)
validate_cross_tenant_fks(order)  # raises ValidationError keyed by field name
order.save()
```

`validate_cross_tenant_fks(instance, *, exclude=None)` skips the foreign keys named in
`exclude`. If you override `clean_fields()` on a model, call
`super().clean_fields(exclude)` or the check is lost.

How it decides:

- The target's tenant is compared with the instance's **own** tenant field, not with the
  active context, so an admin editing another tenant's row is not rejected. Each target is
  read with one small query (per foreign key, per validation) inside `admin_context()`, so
  the result does not depend on the active context, and it works under `STRICT_MODE`.
- Checked: concrete `ForeignKey` and `OneToOneField` fields whose target model has an
  `RLSConstraint` (a proxy's target counts as its concrete model). Skipped: the tenant
  foreign key itself, multi-table-inheritance parent links, empty foreign keys, unsaved
  targets, models without an `RLSConstraint`, and every foreign key while the instance has
  no tenant yet (code that sets the tenant after validation). `RLSTenantModelAdmin` sets an
  implicit tenant on the form's object before the form validates.
- A target that does not exist, or whose tenant is `NULL` (for example a cross-tenant
  admin row on a user model with a nullable tenant foreign key), counts as another
  tenant's row: tenant A cannot see it under RLS either.
- The error is the field's own `invalid` message, the one Django gives a row that does
  not exist, on purpose. A distinct "belongs to another tenant" message would tell a
  tenant user that the id exists in another tenant. On a plain database role in a tenant
  context Django's own foreign key validation already rejects such an id, because RLS hides
  the row; this check adds the protection on superuser and `BYPASSRLS` connections and in
  the admin context, and reports one error, not two.

Django REST Framework serializers do not call `full_clean()`; `RLSTenantSerializerMixin`
runs the same check in `validate()` (see the [DRF guide](drf.md#serializers)).

The only protection that also covers `save()`, bulk writes and raw SQL is a composite
foreign key in the schema, on `(tenant_id, customer_id)` to a unique `(tenant_id, id)`
pair; see [Security](../advanced/security.md#referential-integrity-and-unique-indexes).

## Proxy Models

A proxy of an `RLSProtectedModel` reads and writes its concrete model's table, so the
concrete model's policy applies to it. It gets no tenant field of its own, and the
library treats it as RLS-protected (`STRICT_MODE`, `prefetch_related()`, the
[cross-tenant foreign key check](#cross-tenant-foreign-keys)):

```python
class OpenOrder(Order):
    class Meta:
        proxy = True
```

It inherits `RLSManager`; a manager you give it should subclass `RLSManager` too.

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
