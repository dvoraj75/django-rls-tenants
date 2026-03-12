"""Generic PostgreSQL Row-Level Security primitives.

This layer has **zero imports** from ``django_rls_tenants.tenants``.
It provides reusable building blocks: GUC variable helpers, a migration-aware
``RLSConstraint``, and generic context managers.
"""

from __future__ import annotations

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.rls.context import bypass_flag, rls_context
from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc

__all__ = [
    "RLSConstraint",
    "bypass_flag",
    "clear_guc",
    "get_guc",
    "rls_context",
    "set_guc",
]
