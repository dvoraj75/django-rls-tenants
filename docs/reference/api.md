# API Reference

Auto-generated from source code docstrings.

## Top-Level Exports

The most common symbols are available directly from `django_rls_tenants`:

```python
from django_rls_tenants import (
    AddM2MRLSPolicy,
    RLSConstraint,
    RLSM2MConstraint,
    RLSManager,
    RLSProtectedModel,
    RLSTenantMiddleware,
    TenantQuerySet,
    TenantUser,
    admin_context,
    current_tenant_value_sql,
    safe_tenant_sql,
    tenant_context,
    with_rls_context,
)
```

!!! note "Removed from top-level in v1.2.1"
    Raw state functions (`get_current_tenant_id`, `set_current_tenant_id`,
    `reset_current_tenant_id`, `get_rls_context_active`, `set_rls_context_active`,
    `reset_rls_context_active`) and exception classes (`NoTenantContextError`,
    `RLSConfigurationError`, `RLSTenantError`) are no longer re-exported from the
    top-level package. Import them from their actual modules instead:

    ```python
    from django_rls_tenants.tenants.state import get_current_tenant_id
    from django_rls_tenants.exceptions import NoTenantContextError
    ```

---

## Exceptions

Custom exception hierarchy for precise error handling. All exceptions live in
`django_rls_tenants.exceptions`. Import them from that module directly.

::: django_rls_tenants.exceptions.RLSTenantError

::: django_rls_tenants.exceptions.NoTenantContextError

::: django_rls_tenants.exceptions.RLSConfigurationError

---

## RLS Layer

Generic PostgreSQL Row-Level Security primitives. This layer has **zero imports**
from `tenants/`.

### GUC Helpers

::: django_rls_tenants.rls.guc.set_guc

::: django_rls_tenants.rls.guc.get_guc

::: django_rls_tenants.rls.guc.clear_guc

### Constraints

::: django_rls_tenants.rls.constraints.RLSConstraint

::: django_rls_tenants.rls.constraints.RLSM2MConstraint

### Migration Operations

::: django_rls_tenants.operations.AddM2MRLSPolicy

### Context Managers

::: django_rls_tenants.rls.context.rls_context

::: django_rls_tenants.rls.context.bypass_flag

---

## Tenants Layer

Django multitenancy built on top of the `rls/` primitives.

### Configuration

::: django_rls_tenants.tenants.conf.RLSTenantsConfig

### Models

::: django_rls_tenants.tenants.models.RLSProtectedModel

### Managers

::: django_rls_tenants.tenants.managers.TenantQuerySet

::: django_rls_tenants.tenants.managers.RLSManager

### Context Managers

::: django_rls_tenants.tenants.context.tenant_context

::: django_rls_tenants.tenants.context.admin_context

::: django_rls_tenants.tenants.context.with_rls_context

### Raw SQL

Helpers for scoping hand-written SQL to the current tenant. See the
[Raw SQL guide](../guides/raw-sql.md) for usage and safety notes.

::: django_rls_tenants.tenants.sql.safe_tenant_sql

::: django_rls_tenants.tenants.sql.current_tenant_value_sql

### State

!!! warning "Internal helpers"
    State functions are internal helpers for custom middleware and advanced use cases.
    Prefer `tenant_context()` and `admin_context()` for managing RLS state.
    Import state functions from `django_rls_tenants.tenants.state`, not the top-level package.

#### Tenant ID

::: django_rls_tenants.tenants.state.get_current_tenant_id

::: django_rls_tenants.tenants.state.set_current_tenant_id

::: django_rls_tenants.tenants.state.reset_current_tenant_id

#### RLS Context Active (Strict Mode)

::: django_rls_tenants.tenants.state.get_rls_context_active

::: django_rls_tenants.tenants.state.set_rls_context_active

::: django_rls_tenants.tenants.state.reset_rls_context_active

### Middleware

::: django_rls_tenants.tenants.middleware.RLSTenantMiddleware

### Types

::: django_rls_tenants.tenants.types.TenantUser

### Bypass Helpers

::: django_rls_tenants.tenants.bypass.set_bypass_flag

::: django_rls_tenants.tenants.bypass.clear_bypass_flag

### Testing

::: django_rls_tenants.tenants.testing.rls_bypass

::: django_rls_tenants.tenants.testing.rls_as_tenant

::: django_rls_tenants.tenants.testing.assert_rls_enabled

::: django_rls_tenants.tenants.testing.assert_rls_policy_exists

::: django_rls_tenants.tenants.testing.assert_rls_blocks_without_context

---

## Contrib

Optional integrations with third-party libraries. These are **not** re-exported
from the top-level package; import them from their module.

### Celery

Native Celery integration. Requires the `celery` extra
(`pip install django-rls-tenants[celery]`); import from
`django_rls_tenants.contrib.celery`. See the [Celery Tasks guide](../guides/celery-tasks.md)
for usage, chains/groups, and the `install()` escape hatch.

::: django_rls_tenants.contrib.celery.rls_task

::: django_rls_tenants.contrib.celery.RLSTask

::: django_rls_tenants.contrib.celery.install

::: django_rls_tenants.contrib.celery.uninstall
