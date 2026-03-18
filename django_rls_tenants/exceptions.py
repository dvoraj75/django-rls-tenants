"""Custom exceptions for django-rls-tenants.

All library-specific exceptions inherit from ``RLSTenantError``,
giving callers a single base type to catch any error raised by the
library while still allowing precise handling of specific conditions.
"""

from __future__ import annotations


class RLSTenantError(Exception):
    """Base exception for all django-rls-tenants errors.

    Catch this to handle any error raised by the library.
    """


class NoTenantContextError(RLSTenantError):
    """Query or context operation attempted without an active tenant context.

    Raised when ``STRICT_MODE=True`` and a queryset evaluation is attempted
    without an active ``tenant_context()``, ``admin_context()``,
    ``for_user()``, or ``RLSTenantMiddleware`` context.

    Also raised by ``tenant_context()`` and ``_resolve_user_guc_vars()``
    when a non-admin user has ``rls_tenant_id=None``.
    """


class RLSConfigurationError(RLSTenantError):
    """Invalid or missing RLS configuration.

    Raised when a required configuration key is missing from
    ``settings.RLS_TENANTS`` or when a configuration value is invalid.
    """
