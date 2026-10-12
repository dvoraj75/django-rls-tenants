"""Generic PostgreSQL Row-Level Security primitives.

This layer has **zero imports** from ``django_rls_tenants.tenants``.
It provides reusable building blocks: GUC variable helpers, a migration-aware
``RLSConstraint``, and generic context managers.
"""

from __future__ import annotations

from django_rls_tenants.rls.constraints import RLSConstraint, RLSM2MConstraint
from django_rls_tenants.rls.context import (
    async_bypass_flag,
    async_rls_context,
    bypass_flag,
    rls_context,
)
from django_rls_tenants.rls.guc import (
    aclear_guc,
    aget_guc,
    arestore_guc,
    aset_guc,
    clear_guc,
    get_guc,
    restore_guc,
    set_guc,
)

__all__ = [
    "RLSConstraint",
    "RLSM2MConstraint",
    "aclear_guc",
    "aget_guc",
    "arestore_guc",
    "aset_guc",
    "async_bypass_flag",
    "async_rls_context",
    "bypass_flag",
    "clear_guc",
    "get_guc",
    "restore_guc",
    "rls_context",
    "set_guc",
]
