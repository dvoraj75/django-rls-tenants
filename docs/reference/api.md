# API Reference

Auto-generated from source code docstrings.

## Top-Level Exports

The most common symbols are available directly from `django_rls_tenants`:

```python
from django_rls_tenants import (
    RLSConstraint,
    RLSManager,
    RLSProtectedModel,
    TenantQuerySet,
    TenantUser,
    admin_context,
    tenant_context,
    with_rls_context,
)
```

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
