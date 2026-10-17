"""Database-enforced multitenancy for Django using PostgreSQL Row-Level Security."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

__version__: str = importlib.metadata.version("django-rls-tenants")

__all__ = [
    "AddM2MRLSPolicy",
    "AddRLSPolicy",
    "RLSConstraint",
    "RLSM2MConstraint",
    "RLSManager",
    "RLSProtectedModel",
    "RLSTenantMiddleware",
    "RLSTenantModelAdmin",
    "RawPredicate",
    "SharedWith",
    "TenantField",
    "TenantQuerySet",
    "TenantUser",
    "__version__",
    "admin_context",
    "async_admin_context",
    "async_tenant_context",
    "current_tenant_value_sql",
    "safe_tenant_sql",
    "tenant_context",
    "validate_cross_tenant_fks",
    "with_rls_context",
]

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AddM2MRLSPolicy": ("django_rls_tenants.operations", "AddM2MRLSPolicy"),
    "AddRLSPolicy": ("django_rls_tenants.operations", "AddRLSPolicy"),
    "RLSConstraint": ("django_rls_tenants.rls.constraints", "RLSConstraint"),
    "RLSM2MConstraint": ("django_rls_tenants.rls.constraints", "RLSM2MConstraint"),
    "RLSManager": ("django_rls_tenants.tenants.managers", "RLSManager"),
    "RLSProtectedModel": ("django_rls_tenants.tenants.models", "RLSProtectedModel"),
    "RLSTenantMiddleware": ("django_rls_tenants.tenants.middleware", "RLSTenantMiddleware"),
    "RLSTenantModelAdmin": ("django_rls_tenants.tenants.admin", "RLSTenantModelAdmin"),
    "RawPredicate": ("django_rls_tenants.rls.predicates", "RawPredicate"),
    "SharedWith": ("django_rls_tenants.rls.predicates", "SharedWith"),
    "TenantField": ("django_rls_tenants.rls.predicates", "TenantField"),
    "TenantQuerySet": ("django_rls_tenants.tenants.managers", "TenantQuerySet"),
    "TenantUser": ("django_rls_tenants.tenants.types", "TenantUser"),
    "admin_context": ("django_rls_tenants.tenants.context", "admin_context"),
    "async_admin_context": ("django_rls_tenants.tenants.context", "async_admin_context"),
    "async_tenant_context": ("django_rls_tenants.tenants.context", "async_tenant_context"),
    "current_tenant_value_sql": ("django_rls_tenants.tenants.sql", "current_tenant_value_sql"),
    "safe_tenant_sql": ("django_rls_tenants.tenants.sql", "safe_tenant_sql"),
    "tenant_context": ("django_rls_tenants.tenants.context", "tenant_context"),
    "validate_cross_tenant_fks": (
        "django_rls_tenants.tenants.models",
        "validate_cross_tenant_fks",
    ),
    "with_rls_context": ("django_rls_tenants.tenants.context", "with_rls_context"),
}


def __getattr__(name: str) -> Any:
    """Lazy-import public API symbols to avoid ``AppRegistryNotReady`` during setup."""
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
