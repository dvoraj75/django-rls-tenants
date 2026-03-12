"""Bypass helpers for RLS policies.

Re-exports ``bypass_flag`` from the ``rls`` layer and provides
imperative ``set_bypass_flag`` / ``clear_bypass_flag`` functions.
"""

from __future__ import annotations

from django_rls_tenants.rls.context import bypass_flag
from django_rls_tenants.rls.guc import clear_guc, set_guc

__all__ = ["bypass_flag", "clear_bypass_flag", "set_bypass_flag"]


def set_bypass_flag(
    flag_name: str,
    *,
    is_local: bool = False,
    using: str = "default",
) -> None:
    """Set a bypass flag on the current database connection.

    The flag name should match one of the ``extra_bypass_flags``
    configured on an ``RLSConstraint``::

        set_bypass_flag("rls.is_login_request")

    Args:
        flag_name: GUC variable name (e.g., ``"rls.is_login_request"``).
        is_local: If ``True``, flag is transaction-scoped.
        using: Database alias. Default: ``"default"``.
    """
    set_guc(flag_name, "true", is_local=is_local, using=using)


def clear_bypass_flag(
    flag_name: str,
    *,
    using: str = "default",
) -> None:
    """Clear a bypass flag on the current database connection."""
    clear_guc(flag_name, using=using)
