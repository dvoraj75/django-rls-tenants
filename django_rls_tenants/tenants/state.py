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
from itertools import count
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


_serials = count()


class _Live:
    """A context's GUC values, shared by every copy of the context's ``ContextVar`` values.

    A task, a ``sync_to_async`` hop or a ``copy_context()`` made inside the
    block holds this same object, so ending the block ends the context for
    every copy: work that outlives the block runs without it (fail-closed)
    instead of keeping its tenant or admin rights.

    ``asgiref`` copies ``ContextVar`` values from one context into another
    where they differ (``!=``): into a coroutine ``async_to_sync`` starts, and
    back from a ``sync_to_async`` call into its caller, also when the call is
    cancelled while its function still runs. ``!=`` is therefore defined so
    that a block's holder (``block=True``) never replaces a holder the
    destination has: a cancelled call cannot hand a caller that is inside a
    context (or a request ``RLSTenantMiddleware`` serves on its async path) a
    block that still runs on in another thread. A destination without a
    holder receives it, so ``async_to_sync`` called inside a block inherits
    the block. Only ``RLSTenantMiddleware.process_request`` sets a holder
    that must replace one (``block=False``): ``MiddlewareMixin`` runs it in a
    ``sync_to_async`` hop of its own under ASGI. ``==`` is identity.

    The two copies cannot be told apart, so a destination without a holder
    also receives the block of a ``sync_to_async`` call it cancels while the
    call's function is inside that block: a coroutine outside any context and
    any request (a background job, say) then runs with that block's values
    until the block exits in its thread. Requests hold a holder for their whole
    duration (``middleware._holder_for_the_request``); other coroutines can do
    the same by entering a context of their own around such calls.

    ``serial`` orders holders by creation: a holder that is current where a
    newer one was expected (see :func:`_reset_context_gucs`) is an enclosing
    context brought back by an exit out of order, not a block left open.
    """

    __slots__ = ("alive", "block", "serial", "values")

    def __init__(self, values: ContextGucs | None, *, block: bool) -> None:
        self.values = values
        self.block = block
        self.alive = True
        self.serial = next(_serials)

    def __ne__(self, other: object) -> bool:
        if self is other:
            return False
        if isinstance(other, _Live):
            return not other.block
        return True


class _ContextGucsToken(NamedTuple):
    """What :func:`_set_context_gucs` returns: the ``ContextVar`` token and the holder it set."""

    token: Token[_Live | None]
    live: _Live


# The GUC values of the innermost active context the library set up, or
# ``None`` outside one. Only the library's own entry points set it (the context
# managers, the middleware and ``TenantQuerySet._rls_scope()``), so the guard
# applies the contexts the library set up and nothing else: code that drives the
# public functions above by hand, and writes the GUCs itself, is left alone.
_context_gucs: ContextVar[_Live | None] = ContextVar("rls_context_gucs", default=None)


def _values_of(live: _Live | None) -> ContextGucs | None:
    """Return the GUC values a holder records, or ``None`` if it has ended (or is ``None``)."""
    return live.values if live is not None and live.alive else None


def _get_context_gucs() -> ContextGucs | None:
    """Return the GUC values of the active context, or ``None`` if none is live."""
    return _values_of(_context_gucs.get())


def _current_live() -> _Live | None:
    """Return the holder of the context that is current, live or not, or ``None``."""
    return _context_gucs.get()


def _end_if_newer(live: _Live | None, than: _Live | None) -> None:
    """End ``live``, a holder left current, if it is newer than ``than`` (or ``than`` is ``None``).

    An older one is an enclosing context that an exit out of order (an inner
    block reset below another one) made current again: it stays.
    """
    if live is not None and live is not than and (than is None or live.serial > than.serial):
        live.alive = False


def _end_live_context() -> None:
    """End the context whose values are current, for every copy that holds them."""
    live = _context_gucs.get()
    if live is not None:
        live.alive = False


def _set_context_gucs(values: ContextGucs | None, *, block: bool = True) -> _ContextGucsToken:
    """Record the GUC values of the context being entered.

    Args:
        values: The values the context sets, or ``None`` for a block that runs
            with no context, whatever encloses it (the GUC affinity guard
            then applies none).
        block: ``False`` only for a holder that a ``sync_to_async`` call must
            hand back to its caller (see :class:`_Live`).

    Returns:
        What :func:`_reset_context_gucs` takes to end the context.
    """
    live = _Live(values, block=block)
    return _ContextGucsToken(_context_gucs.set(live), live)


def _reset_context_gucs(entered: _ContextGucsToken) -> None:
    """End the context entered with ``entered`` and restore the enclosing one.

    The context's own holder is ended first, so the context is over for every
    copy even if the reset fails. A newer holder that was current in its place
    (an inner block left without its exit) is ended too, and so is the current
    one when the token was used before. A block entered while this one was
    left current by a suspended generator, and still open when the generator
    is closed, is such a newer holder as well: it is ended (fail-closed), since
    nothing tells it apart from a block nested in this one. A token from another
    ``Context`` (a generator closed there, a hook run in a hop of its own)
    raises ``ValueError`` and leaves that context's current holder alone: it is
    not this block's.

    Args:
        entered: What the corresponding ``_set_context_gucs()`` call returned.

    Raises:
        ValueError: If ``entered`` was created in another ``Context``.
        RuntimeError: If ``entered`` was used before.
    """
    entered.live.alive = False
    current = _context_gucs.get()
    try:
        _context_gucs.reset(entered.token)
    except RuntimeError:
        _end_live_context()
        raise
    _end_if_newer(current, entered.live)


def _hold_context_gucs() -> _ContextGucsToken:
    """Hold the current context's holder until the reset, or a holder of no context if none.

    For a scope that enters no context but needs a holder (see
    ``middleware._holder_for_the_request``): what ``asgiref`` copies in
    replaces it only where :class:`_Live` lets it, and
    :func:`_release_context_gucs` restores the holder that was current
    before, whatever replaced it meanwhile, without ending it.

    Returns:
        What :func:`_release_context_gucs` takes; its ``live`` is the holder held.
    """
    live = _context_gucs.get()
    held = _Live(None, block=True) if live is None else live
    return _ContextGucsToken(_context_gucs.set(held), held)


def _release_context_gucs(held: _ContextGucsToken) -> None:
    """Restore the holder that was current when :func:`_hold_context_gucs` returned ``held``.

    A newer holder current in the held one's place at that point is a context
    block the scope left open, a generator suspended inside one, say: it is
    ended.
    """
    current = _context_gucs.get()
    _context_gucs.reset(held.token)
    _end_if_newer(current, held.live)


def _clear_context_gucs() -> None:
    """End the current context and record none, without a token.

    For cleanup paths that cannot reset through a token (see
    ``middleware._reset_or_clear``) and for the ``request_finished`` safety net.
    """
    _end_live_context()
    _context_gucs.set(None)
