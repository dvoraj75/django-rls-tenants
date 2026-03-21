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
    "RLSConstraint",
    "RLSM2MConstraint",
    "RLSManager",
    "RLSProtectedModel",
    "RLSTenantMiddleware",
    "TenantQuerySet",
    "TenantUser",
    "__version__",
    "admin_context",
    "tenant_context",
    "with_rls_context",
]

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AddM2MRLSPolicy": ("django_rls_tenants.operations", "AddM2MRLSPolicy"),
    "RLSConstraint": ("django_rls_tenants.rls.constraints", "RLSConstraint"),
    "RLSM2MConstraint": ("django_rls_tenants.rls.constraints", "RLSM2MConstraint"),
    "RLSManager": ("django_rls_tenants.tenants.managers", "RLSManager"),
    "RLSProtectedModel": ("django_rls_tenants.tenants.models", "RLSProtectedModel"),
    "RLSTenantMiddleware": ("django_rls_tenants.tenants.middleware", "RLSTenantMiddleware"),
    "TenantQuerySet": ("django_rls_tenants.tenants.managers", "TenantQuerySet"),
    "TenantUser": ("django_rls_tenants.tenants.types", "TenantUser"),
    "admin_context": ("django_rls_tenants.tenants.context", "admin_context"),
    "tenant_context": ("django_rls_tenants.tenants.context", "tenant_context"),
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
