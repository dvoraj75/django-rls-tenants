"""Django multitenancy layer built on top of the generic ``rls/`` primitives.

Provides tenant-aware models, managers, context managers, middleware,
and testing utilities.
"""

from __future__ import annotations

from django_rls_tenants.tenants.bypass import (
    bypass_flag,
    clear_bypass_flag,
    set_bypass_flag,
)
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import (
    admin_context,
    tenant_context,
    with_rls_context,
)
from django_rls_tenants.tenants.managers import RLSManager, TenantQuerySet
from django_rls_tenants.tenants.models import RLSProtectedModel
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    reset_current_tenant_id,
    set_current_tenant_id,
)
from django_rls_tenants.tenants.types import TenantUser

__all__ = [
    "RLSManager",
    "RLSProtectedModel",
    "TenantQuerySet",
    "TenantUser",
    "admin_context",
    "bypass_flag",
    "clear_bypass_flag",
    "get_current_tenant_id",
    "reset_current_tenant_id",
    "rls_tenants_config",
    "set_bypass_flag",
    "set_current_tenant_id",
    "tenant_context",
    "with_rls_context",
]
