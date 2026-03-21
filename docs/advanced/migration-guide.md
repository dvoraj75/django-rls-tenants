# Migration Guide

This guide helps you migrate from other Django multitenancy libraries to
django-rls-tenants.

## From django-tenants

[django-tenants](https://github.com/django-tenants/django-tenants) uses a
schema-per-tenant approach. Migrating to django-rls-tenants means moving from
separate schemas to a single schema with RLS policies.

### Key Differences

| Aspect | django-tenants | django-rls-tenants |
|--------|---------------|-------------------|
| Isolation | Separate PostgreSQL schemas | RLS policies on shared tables |
| Tenant routing | `connection.set_tenant()` | GUC variables via middleware |
| Shared apps | `SHARED_APPS` / `TENANT_APPS` | All apps share one schema |
| Migrations | Run per-schema | Run once (single schema) |
| Raw SQL safety | Yes (schema isolation) | Yes (RLS policies) |
| Database overhead | High (N schemas) | Low (single schema) |

### Migration Steps

1. **Create a tenant FK column** on all tenant-scoped tables:

    ```sql
    ALTER TABLE myapp_order ADD COLUMN tenant_id INTEGER REFERENCES myapp_tenant(id);
    UPDATE myapp_order SET tenant_id = <mapped_tenant_id>;
    ALTER TABLE myapp_order ALTER COLUMN tenant_id SET NOT NULL;
    ```

2. **Replace model inheritance**: change `TenantMixin` to your own tenant model,
   and tenant-scoped models to inherit from `RLSProtectedModel`.

3. **Replace middleware**: swap `TenantMainMiddleware` for `RLSTenantMiddleware`.

4. **Replace tenant routing**: replace `connection.set_tenant()` calls with
   `tenant_context()` or `admin_context()`.

5. **Consolidate schemas** into a single schema (this is the hardest step and is
   project-specific).

6. **Run migrations** to create RLS policies.

7. **Verify**: run `python manage.py check_rls`.

!!! warning
    Schema consolidation is a significant data migration and should be planned carefully.
    Test thoroughly in a staging environment before production.

## From django-multitenant

[django-multitenant](https://github.com/citusdata/django-multitenant) uses ORM-level
query rewriting. Migrating is simpler because you already use a single schema.

### Key Differences

| Aspect | django-multitenant | django-rls-tenants |
|--------|-------------------|-------------------|
| Isolation | ORM query rewriting | RLS policies |
| Raw SQL safety | No | Yes |
| Citus support | Yes | No (standard PostgreSQL) |
| Fail-closed | No | Yes |
| Manager | `TenantManager` | `RLSManager` |

### Migration Steps

1. **Replace model base class**: change `TenantModel` to `RLSProtectedModel`.

    ```python
    # Before (django-multitenant)
    from django_multitenant.models import TenantModel

    class Order(TenantModel):
        tenant_id = 'account_id'
        ...

    # After (django-rls-tenants)
    from django_rls_tenants import RLSProtectedModel

    class Order(RLSProtectedModel):
        ...
    ```

2. **Replace manager calls**: change `set_current_tenant()` to context managers.

    ```python
    # Before
    from django_multitenant.utils import set_current_tenant
    set_current_tenant(tenant)

    # After
    from django_rls_tenants import tenant_context
    with tenant_context(tenant_id=tenant.pk):
        ...
    ```

3. **Replace middleware**: swap the multitenant middleware for `RLSTenantMiddleware`.

4. **Add `TenantUser` properties** to your User model.

5. **Update settings**: replace `MULTI_TENANT` settings with `RLS_TENANTS`.

6. **Run migrations** to create RLS policies.

7. **Verify**: run `python manage.py check_rls`.

## From No Multitenancy

If you are adding multitenancy to an existing single-tenant application:

1. **Create a Tenant model** (see [Tenant Model](../guides/tenant-model.md)).
2. **Add tenant FK** to all data models that need isolation.
3. **Populate the FK** with the appropriate tenant ID for existing data.
4. **Inherit from `RLSProtectedModel`** on those models.
5. **Implement `TenantUser`** on your User model.
6. **Add middleware and settings**.
7. **Run migrations** and verify with `check_rls`.

The most challenging step is populating the tenant FK for existing data. Plan a data
migration that assigns the correct tenant to each existing record.

## Upgrading django-rls-tenants

### From 1.2.0 to 1.2.1

This release has **one breaking change**: internal helpers have been removed from
the top-level package exports.

#### What Changed

1. **Removed from top-level exports**: Raw state functions
   (`get_current_tenant_id`, `set_current_tenant_id`, `reset_current_tenant_id`,
   `get_rls_context_active`, `set_rls_context_active`, `reset_rls_context_active`)
   and exception classes (`NoTenantContextError`, `RLSConfigurationError`,
   `RLSTenantError`) are no longer in `__all__` or importable via
   `from django_rls_tenants import ...`.

2. **Still importable from submodules**: All removed symbols remain available
   from their actual modules.

#### Upgrade Steps

1. **Update imports** that reference the removed symbols from the top-level
   package:

    ```python
    # Before (1.2.0)
    from django_rls_tenants import NoTenantContextError, get_current_tenant_id

    # After (1.2.1)
    from django_rls_tenants.exceptions import NoTenantContextError
    from django_rls_tenants.tenants.state import get_current_tenant_id
    ```

2. **No other changes required.** The context managers (`tenant_context`,
   `admin_context`, `with_rls_context`) remain top-level exports and are the
   recommended API for managing RLS state.

### From 1.1.0 to 1.2.0

This release has **one minor breaking change**: some `ValueError` exceptions have
been replaced with custom exception types.

#### What Changed

1. **Custom exceptions**: The library introduces a custom exception hierarchy in
   `django_rls_tenants.exceptions`. `tenant_context(None)` and
   `_resolve_user_guc_vars()` now raise `NoTenantContextError` instead of
   `ValueError`. `RLSTenantsConfig._get()` now raises `RLSConfigurationError`
   instead of `ValueError`. If you catch `ValueError` from these functions,
   update your except clauses. Both are subclasses of `RLSTenantError`, which
   is a subclass of `Exception`.

2. **Multi-database GUC support**: The middleware now sets GUC variables on all
   database aliases listed in `RLS_TENANTS["DATABASES"]` (default: `["default"]`).
   No changes needed for single-database setups.

3. **Strict mode** (`STRICT_MODE=True`): An opt-in setting that raises
   `NoTenantContextError` when queries execute without an active RLS context.
   Off by default -- existing behavior is unchanged.

4. **New public API**: `get_rls_context_active()`, `set_rls_context_active()`,
   `reset_rls_context_active()` for tracking whether an RLS context is active.
   These are primarily used internally by strict mode but are available for
   custom middleware implementations.

#### Upgrade Steps

1. **Update the package**:

    ```bash
    pip install --upgrade django-rls-tenants
    ```

2. **Update exception handling** (if applicable):

    ```python
    # Before (1.1.0)
    from django_rls_tenants import tenant_context
    try:
        with tenant_context(tenant_id=None):
            ...
    except ValueError:
        ...

    # After (1.2.0)
    from django_rls_tenants import tenant_context
    from django_rls_tenants.exceptions import NoTenantContextError
    try:
        with tenant_context(tenant_id=None):
            ...
    except NoTenantContextError:
        ...
    ```

3. **Optional: enable multi-database support**:

    ```python
    RLS_TENANTS = {
        "TENANT_MODEL": "myapp.Tenant",
        "DATABASES": ["default", "replica"],
    }
    ```

4. **Optional: enable strict mode**:

    ```python
    RLS_TENANTS = {
        "TENANT_MODEL": "myapp.Tenant",
        "STRICT_MODE": True,
    }
    ```

5. **Verify**: run `python manage.py check` and `python manage.py check_rls`.

### From 1.0.0 to 1.1.0

This release has **no breaking changes**. All existing code continues to work without
modification.

#### What Changed

1. **RLS policy SQL**: `RLSConstraint` now generates `CASE WHEN` policies instead of
   `OR`-based policies, improving readability and clarifying the evaluation structure.
   (The primary performance improvement comes from auto-scoping below, which enables
   composite index usage.)

2. **Automatic query scoping**: `RLSManager.get_queryset()` now adds
   `WHERE tenant_id = X` automatically when a tenant context is active (via
   `tenant_context()`, `admin_context()`, or `RLSTenantMiddleware`). This enables
   composite indexes and eliminates sequential scan penalties at scale.

3. **New public API**: `get_current_tenant_id()`, `set_current_tenant_id()`, and
    `reset_current_tenant_id()` are available for custom middleware and management
    commands that need direct access to the auto-scope state.

#### Upgrade Steps

1. **Update the package**:

    ```bash
    pip install --upgrade django-rls-tenants
    ```

2. **Generate a new migration** to update the RLS policy SQL:

    ```bash
    python manage.py makemigrations
    python manage.py migrate
    ```

    This replaces the `OR`-based policy with the `CASE WHEN` structure. The migration
    is safe to run on a live database -- it drops and recreates the policy in a single
    DDL statement.

3. **Verify**: run `python manage.py check_rls`.

#### Behavioral Notes

- `for_user()` continues to work exactly as before.
- Auto-scoping activates automatically -- no code changes required. If both
  auto-scoping and `for_user()` are active simultaneously, the query gets two
  redundant `WHERE tenant_id = X` clauses. This is by design for defense-in-depth;
  the cost of the double equality check per row is negligible.
- `TenantQuerySet.select_related()` now auto-propagates tenant filters to joined
  RLS-protected tables when a tenant context is active.
