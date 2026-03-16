"""Thread-safe and async-safe tenant state for automatic query scoping.

Uses ``contextvars.ContextVar`` to store the current tenant ID so that
``RLSManager.get_queryset()`` can automatically add an ORM-level
``WHERE tenant_id = X`` filter. This enables PostgreSQL to use composite
indexes, eliminating the sequential scan penalty that occurs when relying
solely on RLS ``current_setting()`` calls (which are not leakproof).

The state is set/restored by ``tenant_context()``, ``admin_context()``,
and ``RLSTenantMiddleware``. Users should not need to call these functions
directly -- use the context managers or middleware instead.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_current_tenant_id: ContextVar[int | str | None] = ContextVar(
    "rls_current_tenant_id", default=None
)


def get_current_tenant_id() -> int | str | None:
    """Return the current tenant ID, or ``None`` if no tenant context is active.

    Returns:
        The tenant ID set by the innermost active ``tenant_context()`` or
        middleware, or ``None`` if in admin mode or no context is active.
    """
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: int | str | None) -> Token[int | str | None]:
    """Set the current tenant ID for automatic query scoping.

    Returns a token that can be passed to ``reset_current_tenant_id()``
    to restore the previous value (for nesting support).

    Args:
        tenant_id: The tenant PK, or ``None`` to clear (admin/no context).

    Returns:
        A ``Token`` for restoring the previous value.
    """
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token: Token[int | str | None]) -> None:
    """Restore the previous tenant ID using a token from ``set_current_tenant_id()``.

    Args:
        token: The token returned by the corresponding ``set_current_tenant_id()`` call.
    """
    _current_tenant_id.reset(token)
