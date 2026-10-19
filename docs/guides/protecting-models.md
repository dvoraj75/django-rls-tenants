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

M2M fields on the models that declare their own RLS policy -- `RLSProtectedModel`
subclasses and any model whose `Meta.constraints` list an `RLSConstraint` -- are
automatically detected, and a related model of either kind counts as a protected side.
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
  `RLSConstraint` or is listed in
  [`EXTERNAL_MODELS`](#tables-you-do-not-own) (a proxy's target counts as its concrete
  model; a listed target is compared through its own tenant field). Skipped: the tenant
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

## Tables You Do Not Own

*Added in 1.6.0.*

A model of a third-party app cannot inherit `RLSProtectedModel` or carry an
`RLSConstraint`: its `Meta` and its migrations are not yours. If its table has a foreign
key to your tenant model, you can still give the table the policy `RLSConstraint` would
create, in two steps:

1. Add an `AddRLSPolicy` operation to a migration of **your own** app. It creates the
   policy in the database.
2. List the model in `RLS_TENANTS["EXTERNAL_MODELS"]`, so the library treats it as
   RLS-protected at runtime (`check_rls`, the
   [cross-tenant foreign key check](#cross-tenant-foreign-keys), `select_related()` and
   `prefetch_related()`).

The example assumes a third-party app `library` whose `Book` model has a foreign key to
your tenant model, named `tenant`. The operation adds no column, so the table must already
have it.

```python title="myapp/migrations/0007_library_book_rls_policy.py"
from django.db import migrations

from django_rls_tenants import AddRLSPolicy


class Migration(migrations.Migration):
    dependencies = [
        ("myapp", "0006_previous"),
        # The migration of the third-party app that creates Book and its tenant FK.
        ("library", "0001_initial"),
    ]

    operations = [
        AddRLSPolicy("Book", app_label="library"),
    ]
```

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "EXTERNAL_MODELS": {"library.Book": "tenant"},
}
```

Run `python manage.py migrate`, then `python manage.py check_rls`. It lists the table with
the others and verifies that RLS is enabled, forced and has a policy:

```
  Book (library_book): library_book_tenant_isolation_policy
```

You write this migration by hand: `makemigrations` does not detect it, and
`check_rls --generate` writes only M2M migrations. The setting exists because the library
cannot add an `RLSConstraint` to the third-party model's `Meta` at startup: the migration
autodetector would then write a migration for the third-party app.

### The operation

`AddRLSPolicy(model_name, *, field="tenant", app_label=None, tenant_pk_type=None,
guc_tenant_var=None, guc_admin_var=None, extra_bypass_flags=None)` is exported from the
package root (`from django_rls_tenants import AddRLSPolicy`).

| Argument | Meaning |
|----------|---------|
| `model_name` | The model's name, `"Book"`. |
| `field` | The name of the model's tenant `ForeignKey` (a `OneToOneField` is accepted). Default `"tenant"`. The policy reads the field's real column from the migration state, so `db_column` is respected. A column other than the lower-case `<field>_id` Django names (a `db_column`, which may be a reserved word, or a name with an upper-case letter) is quoted. |
| `app_label` | The model's app. Without it, the app of the migration the operation is in; for a third-party model, pass it. |
| `tenant_pk_type`, `guc_tenant_var`, `guc_admin_var` | As on [`RLSConstraint`](../reference/api.md). When unset, they come from `RLS_TENANTS` (see below). |
| `extra_bypass_flags` | Additional GUCs that bypass the `USING` clause but not `WITH CHECK`, as on `RLSConstraint` (see [Extra Bypass Flags](#extra-bypass-flags)). |

The policy is the one `RLSConstraint` creates: the same SQL text, from the same builder.
It is named `<table>_tenant_isolation_policy`, enables and **forces** RLS on the table,
lets the admin flag (and the extra bypass flags) through in `USING`, and only the admin
flag in `WITH CHECK`. The creating `DO` block skips a table that already has a policy of
that name, so applying the operation twice changes nothing.

- **Dependencies.** The migration must list, in `dependencies`, the third-party migration
  that creates the model and its tenant foreign key. A model or field missing from the
  migration state raises `ValueError` at migrate time, naming what to add. A proxy model,
  a field that is not a `ForeignKey`, or a foreign key to another model than
  `TENANT_MODEL` raises `ValueError` too.
- **Reversible.** Migrating backwards drops the policy and disables RLS on the table
  (`NO FORCE` and `DISABLE`).
- **Database only.** `state_forwards()` changes nothing. `sqlmigrate` prints the SQL, and
  `makemigrations`-style listings show `+ Add RLS policy to library.Book`.
- **Routers and unmanaged models.** Like Django's own operations, it does nothing on a
  database that your router does not let the **model's** app migrate to (the router is
  asked about the target model, not the migration's app), and nothing for an unmanaged
  (`managed = False`) or swapped-out model.
- **Models with an `RLSConstraint`.** While the model has an `RLSConstraint` in the
  migration state (the third-party app adopted one), the operation does nothing in either
  direction: the constraint's policy has the same name, and migrating backwards must not
  drop it. If the operation ran first, the table keeps its policy, since the
  constraint's `DO` block skips a policy that exists.
- **Table names.** A `db_table` with a quote, a backslash or a dollar sign (the
  `'schema"."table'` idiom among them) raises `ValueError`: the policy SQL cannot hold it.

!!! warning "GUC names and the tenant type are read when the migration runs"
    `guc_tenant_var`, `guc_admin_var` and `tenant_pk_type`, when you leave them out, are
    read from `RLS_TENANTS` (`GUC_PREFIX`, `TENANT_PK_TYPE`) **when the migration runs**,
    not when you write it, and the migration file stores only the arguments you gave.
    Changing the settings later does not rewrite a policy that is applied, and the output
    of `sqlmigrate` depends on the settings in force. `AddM2MRLSPolicy` and `RLSConstraint`
    have the literal defaults `rls.current_tenant`, `rls.is_admin` and `int` instead. Pass
    the three arguments explicitly if the migration must not follow the settings.

### What `EXTERNAL_MODELS` gives the model

The setting maps the model's label to the name of its tenant foreign key (see
[`EXTERNAL_MODELS`](../getting-started/configuration.md#external_models)). A listed model
is treated as RLS-protected by:

- **`check_rls`**, which checks its table and prints the fix for a failure, built from the
  `AddRLSPolicy` operations on disk (see
  [Management Commands](management-commands.md#what-it-checks));
- **the [cross-tenant foreign key check](#cross-tenant-foreign-keys)** of models with a
  foreign key to it, which compares the target's tenant, read through the listed field;
- **`select_related()` and the `prefetch_related()` tenant filter** on joins into it,
  which filter on the listed model's own tenant column (`owner_id`, say), not on
  `TENANT_FK_FIELD`;
- **system checks** `W012` (its unique fields and constraints that span tenants; see
  [`UNIQUE_ACROSS_TENANTS`](../getting-started/configuration.md#unique_across_tenants)),
  `W015` and the new `W016`, which reports an entry the library cannot use.

A proxy of a listed model is treated as protected through its concrete model. An entry
that names a proxy itself is ignored (`W016`).

### What it does not give

The model keeps its own manager, so the application-level protections of
`RLSProtectedModel` do not apply to **queries on the external model itself**. For those,
the database policy is the only isolation:

- No automatic `WHERE tenant_id = X` filter, no `for_user()`, and no `STRICT_MODE` guard.
  Without a context, a query on the model returns no rows (fail closed), and the planner
  cannot use the tenant index through the policy alone (see the
  [Performance guide](performance.md)). Add the tenant filter yourself where it matters.
- No tenant is filled in on new rows. Pass the tenant foreign key when you create a row;
  outside the admin context, `WITH CHECK` rejects a row whose tenant is not the
  context's.
- No coverage of the model's many-to-many through tables. `register_m2m_rls()` and the
  `check_rls` M2M discovery look only at models that declare their own policy (an
  `RLSProtectedModel` subclass or an `RLSConstraint`), never at an `EXTERNAL_MODELS`
  entry. An M2M field from
  an `RLSProtectedModel` to an external model gets a policy that checks the protected side
  only.
- `check_rls --generate` does not write `AddRLSPolicy` migrations, only M2M ones.

See [Security](../advanced/security.md#models-you-do-not-own) for the same limits from the
isolation point of view.

## Sharing Rows Across Tenants

*Added in 1.6.0.*

By default a tenant reads its own rows and nothing else. `extra_predicates` on
`RLSConstraint` lets a tenant **read** chosen rows of other tenants too: a document its
owner shared with it, or a row that names it as a partner. It never lets the tenant change
those rows.

```python title="notes/models.py"
from django.db import models

from django_rls_tenants import (
    RLSConstraint,
    RLSProtectedModel,
    SharedWith,
    TenantField,
)


class Note(RLSProtectedModel):
    title = models.CharField(max_length=255)
    partner = models.ForeignKey(
        "myapp.Tenant", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="partner_notes",
    )

    class Meta(RLSProtectedModel.Meta):
        # Replace the inherited constraint with one of the same name.
        constraints = [
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
                extra_predicates=[
                    SharedWith(through="notes.NoteShare", fk="note", tenant_fk="shared_with"),
                    TenantField("partner"),
                ],
            ),
        ]


class NoteShare(RLSProtectedModel):
    """Shares `note` with the tenant in `shared_with`; `tenant` is the note's owner."""

    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name="shares")
    shared_with = models.ForeignKey(
        "myapp.Tenant", on_delete=models.CASCADE, related_name="received_shares",
    )

    class Meta(RLSProtectedModel.Meta):
        constraints = [
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
                # The tenant a note is shared with must be able to read the share.
                extra_predicates=[TenantField("shared_with")],
            ),
        ]
        indexes = [models.Index(fields=["note", "shared_with"])]
```

Run `makemigrations`. Django writes a `RemoveConstraint` and an `AddConstraint` for a model
whose constraint changes, which drops and recreates its policies. A new model carries the
constraint in its `CreateModel`.

### The predicates

All three are importable from `django_rls_tenants` (and from `django_rls_tenants.rls`,
with the `Predicate` base class). They are deconstructible, so migrations serialize them.
Each is built from the model the policy is created on; an argument that cannot be
interpolated safely, or a model or field that does not resolve, raises `ValueError`.

| Predicate | Matches a row when |
|-----------|--------------------|
| `SharedWith(through="app.Model", fk="note", tenant_fk="shared_with")` | a row of `through` points at it with `fk` (a `ForeignKey` of `through` to this model, or a proxy of it) and at the current tenant with `tenant_fk` (a `ForeignKey` of `through`) |
| `TenantField("partner")` | its foreign key `partner` is the current tenant: a second tenant column |
| `RawPredicate("...")` | the SQL expression you write is true |

`SharedWith` becomes this `EXISTS` subquery (the through table is aliased, so the
protected table's own columns stay reachable by its name):

```sql
EXISTS (SELECT 1 FROM "notes_noteshare" AS "rls_shared"
        WHERE "rls_shared"."note_id" = "notes_note"."id"
        AND "rls_shared"."shared_with_id" = nullif(
            (SELECT current_setting('rls.current_tenant', true)), '')::int)
```

`TenantField` becomes `"notes_note"."partner_id" = nullif((SELECT current_setting(...)), '')::int`.
With no tenant context, the tenant value is `NULL`, so neither matches anything. The SQL
text uses the constraint's `guc_tenant_var` and `tenant_pk_type`. Identifiers are double-quoted;
a table or column name with a quote, a backslash, a dollar sign or a NUL is refused. So is a
table name of 55 bytes or more: PostgreSQL cuts an identifier to 63 bytes, which would give
the sharing policy the tenant policy's name, and it would never be created (`migrate` raises
`ValueError`; system check `W017` reports it first).

`RawPredicate` is SQL you own. It is used as written, in parentheses, and only text that
would break out of the policy statement is refused: an empty string, `;`, `--`, `/*`, `$` and
a NUL. Write a `%` (a `LIKE` pattern) once, as in SQL: the library doubles it for Django's
schema editor, which composes the policy DDL with its parameters. `SharedWith`'s `tenant_fk`
and `TenantField`'s `field` must be foreign keys to the tenant model; system check `W019`
reports one that points elsewhere, which would compare that model's keys with the tenant's.
It must compare with the tenant itself, through the GUC the policies read:

```python
RawPredicate("region = 'eu' AND partner_id = nullif(current_setting('rls.current_tenant', true), '')::int")
```

!!! danger "A raw predicate that ignores the tenant opens rows to everyone"
    `RawPredicate("is_public")` makes every public row readable by every tenant, and by a
    session with no tenant context. The library does not inspect the expression.

### What the policy changes

The predicates do **not** change the tenant policy `<table>_tenant_isolation_policy`: its
SQL is byte-identical to a model without predicates. They go into a second, permissive
`FOR SELECT` policy, `<table>_tenant_sharing_policy`, whose `USING` is the predicates joined
by `OR`. PostgreSQL ORs the permissive policies that apply to a command, so:

| Command | Policies that apply | Result for a row shared with the tenant |
|---------|--------------------|------------------------------------------|
| `SELECT` | tenant policy OR sharing policy | readable |
| `UPDATE`, `DELETE` | tenant policy | matches nothing: 0 rows affected |
| `INSERT` | tenant policy | `WITH CHECK` rejects a row of another tenant |
| `SELECT ... FOR UPDATE` / `FOR SHARE` | tenant policy | the shared row is not returned |

A tenant therefore cannot update, delete, lock or take over a shared row (`UPDATE ... SET
tenant_id = <me>` matches nothing). Putting the predicates into the existing policy's
`USING` would not give this: `WITH CHECK` only inspects the new row, so the receiving
tenant could delete the row or move it to itself.

The migration operations drop the sharing policy first when reversed, and a model without
predicates creates no second policy. `check_rls --verbose` lists the policy with its
command (`notes_note_tenant_sharing_policy [SELECT]`) and its `USING` expression.

### Reading shared rows: `including_shared()`

The default manager adds `WHERE tenant_id = X` to every query inside a tenant context
(and `for_user()` adds it too), which would hide the shared rows. `including_shared()`
returns a queryset without those filters, so the policies alone decide:

```python
with tenant_context(tenant_b.pk):
    Note.objects.all()                    # B's own notes
    Note.objects.including_shared()       # B's own notes + notes shared with B
    Note.objects.including_shared().for_user(user_b)
    note.shares.including_shared()        # related manager: the relation filter stays
```

- It is a **manager** method. Call it first, then `filter()`, `for_user()` and the rest:
  `including_shared()` is not a queryset method, and `for_user()` after it adds no tenant
  filter.
- The queryset adds no tenant filter of any kind: not the automatic one, not `for_user()`'s,
  not the ones `select_related()` adds to joined RLS-protected tables, and not the
  `prefetch_related()` rewrite (see the [Performance guide](performance.md#prefetch_related)).
  Rows a related manager already prefetched were read with the tenant filter and are not
  reused.
- With no tenant context the policies match no rows of a tenant; in `admin_context()` it
  reads everything; under `STRICT_MODE` it needs a context like any queryset.
- On a model whose `RLSConstraint` has no `extra_predicates` (the concrete model's, for a
  proxy), it raises `RLSConfigurationError` with a hint, so it is never a way to switch the
  tenant filter off.
- **It is read-only.** `update()`, `delete()`, `bulk_update()`, `update_or_create()` and
  `select_for_update()` (and the async variants of those that have one) raise `TypeError`.
  The database would skip shared rows in an `UPDATE` and `DELETE`, but Django's `delete()`
  runs a collector first: it would fire the delete signals of shared rows and
  `CASCADE`-delete the current tenant's own rows that point at them, while the shared row
  itself survives. Call those methods on `Note.objects`, not on an `including_shared()` queryset.
  A queryset combined with one (`qs | Note.objects.including_shared()`, `&`, `^`) is
  read-only too.

!!! warning "Methods on an instance are not blocked"
    `instance.delete()` on a row of another tenant that you read through
    `including_shared()` runs the same collector cascade on your own related rows, and
    `instance.save()` fails at the database (the `UPDATE` affects no row, and the `INSERT`
    Django then tries violates the policy). Do not call them on shared rows.

Relations of a shared row are not shared. An auto-created many-to-many through table
keeps its policy, which requires both sides in the tenant, and a reverse relation
(`note.shares.all()`) goes through the related model's own manager and policy.

### The share model must be readable by the tenant it shares with

The `EXISTS` subquery reads the `through` table, and **that table's own RLS policy applies
inside it**. If `NoteShare` is protected by the owner's tenant (the default `tenant` field),
the tenant a note is shared with cannot see the share row, so `SharedWith` never matches.
Give the share model `TenantField("shared_with")` in its own `extra_predicates`, as above,
or make its tenant field the receiving tenant.

System check [`W018`](../troubleshooting.md#w018-a-sharedwith-predicate-never-matches) reports
this: a `SharedWith` whose `through` model has an `RLSConstraint` on another field than
`tenant_fk` and no `TenantField(tenant_fk)` among its `extra_predicates`. It does not
recognize a `RawPredicate` that makes the rows visible: silence the warning then.
[`W017`](../troubleshooting.md#w017-an-extra-predicate-cannot-be-built) reports a predicate
that cannot be built, which `migrate` would fail on too.

### Migration notes

- `extra_predicates` is also an argument of `AddRLSPolicy` (see
  [Tables You Do Not Own](#tables-you-do-not-own)): the sharing policy is created after the
  tenant policy, and the operation's `through` model must already be in the migration state
  (a dependency, or an earlier operation). Unset GUC names and the tenant PK type are read
  from `RLS_TENANTS` when the migration runs, like its other options.
- With `RLSConstraint`, the sharing SQL is built when Django renders the statement. In a
  migration that creates the protected model and its share model, Django defers the
  constraint SQL of `CreateModel` to the end of the migration, so a share model created
  later in the same migration is found. Elsewhere (an `AddConstraint`), the `through` model
  must already exist in the migration state.
- A model without predicates keeps serializing exactly as before: `deconstruct()` omits an
  empty list.
- An external model (`EXTERNAL_MODELS`) keeps its own manager and has no
  `including_shared()`. Its manager adds no tenant filter, so a row its sharing policy
  shares is already visible to its normal queries.

!!! tip "Index the share table"
    `including_shared()` leaves `tenant_id = X` out of the query, and the sharing policy
    runs one `EXISTS` per candidate row. Index the `through` table on `(fk, tenant_fk)`
    (`models.Index(fields=["note", "shared_with"])`). See the
    [Performance guide](performance.md#sharing-rows).

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
