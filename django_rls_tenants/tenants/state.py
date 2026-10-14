"""Tenant state for automatic query scoping and strict mode.

Relies on Python's ``contextvars.ContextVar`` for thread and async-task
isolation.

Uses ``contextvars.ContextVar`` to store the current tenant ID so that
``RLSManager.get_queryset()`` can automatically add an ORM-level
``WHERE tenant_id = X`` filter. This enables PostgreSQL to use composite
indexes, eliminating the sequential scan penalty that occurs when relying
solely on RLS ``current_setting()`` calls (which are not leakproof).

A second ``ContextVar`` (``_rls_context_active``) tracks whether *any*
RLS context is active (tenant or admin). This is needed by strict mode
to distinguish "no context" from "admin context" -- both have
``_current_tenant_id=None``, but only the former should raise.

A third, private ``ContextVar`` (``_context_gucs``) holds the admin and
tenant GUC values the library applied for the active context, ended for every
copy of the context when its block exits. The GUC affinity guard
(``tenants/affinity.py``) applies those values on whichever connection a
statement runs on.

The state is set/restored by ``tenant_context()``, ``admin_context()``,
and ``RLSTenantMiddleware``. Users should not need to call these functions
directly -- use the context managers or middleware instead.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Final, NamedTuple

# ---- Tenant ID state ----

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


# ---- RLS context active flag (strict mode) ----

_rls_context_active: ContextVar[bool] = ContextVar("rls_context_active", default=False)


def get_rls_context_active() -> bool:
    """Return whether an RLS context is currently active.

    An RLS context is active when ``tenant_context()``,
    ``admin_context()``, or ``RLSTenantMiddleware`` has established
    a context for the current execution scope. Used by strict mode
    to distinguish "no context" from "admin context".

    Returns:
        ``True`` if an RLS context is active.
    """
    return _rls_context_active.get()


def set_rls_context_active(active: bool) -> Token[bool]:
    """Set the RLS context active flag.

    Args:
        active: Whether an RLS context is active.

    Returns:
        A ``Token`` for restoring the previous value.
    """
    return _rls_context_active.set(active)


def reset_rls_context_active(token: Token[bool]) -> None:
    """Restore the previous RLS context active state.

    Args:
        token: The token returned by ``set_rls_context_active()``.
    """
    _rls_context_active.reset(token)


# ---- GUC values of the active context (GUC affinity guard) ----


class ContextGucs(NamedTuple):
    """The admin and tenant GUC values an RLS context sets.

    Internal: the library's entry points record them for the GUC affinity
    guard (``tenants/affinity.py``).
    """

    is_admin: str
    current_tenant: str

    @classmethod
    def for_tenant(cls, tenant_id: int | str) -> ContextGucs:
        """Return the values of ``tenant_context(tenant_id)``."""
        return cls("false", str(tenant_id))


# The values of ``admin_context()``: the tenant GUC is cleared, not a sentinel.
ADMIN_GUCS: Final = ContextGucs("true", "")


class _Live:
    """A context's GUC values, shared by every copy of the context's ``ContextVar`` values.

    A task, a ``sync_to_async`` hop or a ``copy_context()`` made inside the
    block holds this same object, so ending the block ends the context for
    every copy: work that outlives the block runs without it (fail-closed)
    instead of keeping its tenant or admin rights.
    """

    __slots__ = ("alive", "values")

    def __init__(self, values: ContextGucs) -> None:
        self.values = values
        self.alive = True


# The GUC values of the innermost active context the library set up, or
# ``None`` outside one. Only the library's own entry points set it (the context
# managers, the middleware and ``TenantQuerySet._rls_scope()``), so the guard
# applies the contexts the library set up and nothing else: code that drives the
# public functions above by hand, and writes the GUCs itself, is left alone.
_context_gucs: ContextVar[_Live | None] = ContextVar("rls_context_gucs", default=None)


def _get_context_gucs() -> ContextGucs | None:
    """Return the GUC values of the active context, or ``None`` if none is live."""
    live = _context_gucs.get()
    return live.values if live is not None and live.alive else None


def _end_live_context() -> None:
    """End the context whose values are current, for every copy that holds them."""
    live = _context_gucs.get()
    if live is not None:
        live.alive = False


def _set_context_gucs(values: ContextGucs) -> Token[_Live | None]:
    """Record the GUC values of the context being entered.

    Args:
        values: The values the context sets.

    Returns:
        A ``Token`` for :func:`_reset_context_gucs`.
    """
    return _context_gucs.set(_Live(values))


def _reset_context_gucs(token: Token[_Live | None]) -> None:
    """End the context entered with ``token`` and restore the enclosing one.

    Args:
        token: The token returned by the corresponding ``_set_context_gucs()`` call.
    """
    _end_live_context()
    _context_gucs.reset(token)


def _clear_context_gucs() -> None:
    """End the current context and record none, without a token.

    For cleanup paths that cannot reset through a token (see
    ``middleware._reset_or_clear``) and for the ``request_finished`` safety net.
    """
    _end_live_context()
    _context_gucs.set(None)
