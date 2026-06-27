"""Actionable hint constants for tenant-layer errors.

These strings are passed as ``hint=`` to :class:`RLSTenantError` subclasses
so every error tells the user how to fix it. They live in the ``tenants``
layer (not ``exceptions.py``) because they name tenant-layer concepts --
``tenant_context()``, ``admin_context()``, ``for_user()``, and the
``TenantUser`` attributes -- which keeps :mod:`django_rls_tenants.exceptions`
framework-agnostic.

The same constants are reused wherever the library raises the matching
condition (context managers, the ``@with_rls_context`` decorator, strict
mode, and -- in later v1.3.0 features -- Celery tasks and the admin) so the
guidance stays consistent.
"""

from __future__ import annotations

HINT_NO_CONTEXT = (
    "Establish an RLS context before the query: wrap it in "
    "tenant_context(tenant_id) or admin_context(), scope the queryset with "
    ".for_user(user), or enable RLSTenantMiddleware so each request sets the "
    "context automatically."
)
"""No RLS context is active where one is required (e.g. strict mode)."""

HINT_USER_NO_TENANT = (
    "Assign the user to a tenant (set rls_tenant_id) or mark them as a "
    "cross-tenant admin (is_tenant_admin=True). A non-admin TenantUser must "
    "belong to exactly one tenant."
)
"""A non-admin ``TenantUser`` has ``rls_tenant_id=None``."""

HINT_TENANT_ID_NONE = (
    "Pass a concrete tenant primary key to tenant_context(tenant_id). For "
    "cross-tenant admin access that bypasses tenant scoping, use "
    "admin_context() instead."
)
"""``tenant_context(None)`` was called with no tenant id."""
