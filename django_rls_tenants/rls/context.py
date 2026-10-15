"""Generic RLS context managers.

Provides ``rls_context`` for setting/clearing arbitrary GUC variables,
and ``bypass_flag`` for toggling boolean bypass flags within a
transaction-scoped context. Both support save/restore nesting in both
GUC modes (``set_config`` and ``SET LOCAL``). ``async_rls_context`` and
``async_bypass_flag`` are the ``async with`` counterparts.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import DatabaseError, connections

from django_rls_tenants.rls.guc import get_guc, restore_guc, set_guc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping

logger = logging.getLogger("django_rls_tenants")


def restore_gucs(
    previous: Mapping[str, str | None],
    *,
    is_local: bool = False,
    using: str = "default",
    exc_pending: bool = False,
) -> bool:
    """Restore several GUCs to the values saved before a context block.

    Shared exit path for every context manager in the library. Restoring
    inside the same scope the block used (``is_local``) is what makes
    nesting work under ``SET LOCAL``: PostgreSQL does not undo a
    ``SET LOCAL`` when a Python block exits, only at transaction end, so the
    outer value has to be re-issued explicitly.

    When the block is exiting because of an exception (``exc_pending``) and
    that exception left the transaction aborted, PostgreSQL rejects every
    further statement with ``InFailedSqlTransaction``. Letting that error
    escape would replace the user's real exception (an ``IntegrityError``,
    a ``DataError``, ...) with "current transaction is aborted", so a
    ``DatabaseError`` from the restore is logged and swallowed in that case.
    This is safe: PostgreSQL rolls back ``set_config()`` and ``SET LOCAL``
    changes together with the aborted transaction or savepoint, so the
    rollback restores what the failed statement could not. On a normal exit
    a restore failure propagates as usual.

    Django's own "needs rollback" flag does not stop the restore. An
    exception leaving an inner ``atomic(savepoint=False)`` block sets it,
    and Django then refuses every query with ``TransactionManagementError``
    even though PostgreSQL's transaction is healthy. If the caller catches
    that exception and clears the flag with ``transaction.set_rollback(False)``,
    the transaction commits, and with it any GUC left unrestored. The flag
    is therefore lifted for the restore statements and put back afterwards;
    a transaction that PostgreSQL really aborted still fails them, as above.

    Args:
        previous: Mapping of GUC name to the value read before the block.
        is_local: Scope the block used; ``True`` re-issues ``SET LOCAL``.
        using: Database alias. Default: ``"default"``.
        exc_pending: ``True`` when called while an exception is propagating.

    Returns:
        ``True`` if every GUC was restored, ``False`` if a failure was
        skipped because ``exc_pending`` was set.
    """
    if not previous:
        return True
    conn = connections[using]
    needs_rollback = conn.needs_rollback
    conn.needs_rollback = False
    restored = True
    try:
        for name, prev in previous.items():
            try:
                restore_guc(name, prev, is_local=is_local, using=using)
            except DatabaseError:
                if not exc_pending:
                    raise
                restored = False
                logger.debug(
                    "Skipping GUC restore for %s: the transaction is already aborted and "
                    "will be rolled back; the original exception is propagated instead.",
                    name,
                    exc_info=True,
                )
    finally:
        conn.needs_rollback = needs_rollback
    return restored


@contextmanager
def rls_context(
    variables: dict[str, str],
    *,
    is_local: bool = False,
    using: str = "default",
) -> Iterator[None]:
    """Set multiple GUC variables for the duration of a block.

    Saves and restores previous values on exit (supports nesting) in both
    GUC modes. With ``is_local=True`` the previous values are re-issued with
    ``SET LOCAL`` so an enclosing block keeps its scope until the
    transaction ends.

    Args:
        variables: Dict of GUC variable names to values.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.
    """
    previous_values: dict[str, str | None] = {
        name: get_guc(name, using=using) for name in variables
    }

    exc_pending = False
    try:
        for name, value in variables.items():
            set_guc(name, value, is_local=is_local, using=using)
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        restore_gucs(previous_values, is_local=is_local, using=using, exc_pending=exc_pending)


@contextmanager
def bypass_flag(
    flag_name: str,
    *,
    is_local: bool = False,
    using: str = "default",
) -> Iterator[None]:
    """Temporarily set a GUC bypass flag to ``'true'``.

    Saves and restores the previous value on exit (supports nesting) in
    both GUC modes.

    Usage::

        with bypass_flag("rls.is_login_request"):
            user = User.objects.get(email=email)
    """
    previous = get_guc(flag_name, using=using)

    set_guc(flag_name, "true", is_local=is_local, using=using)
    exc_pending = False
    try:
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        restore_gucs(
            {flag_name: previous},
            is_local=is_local,
            using=using,
            exc_pending=exc_pending,
        )


def _enter_gucs(
    variables: Mapping[str, str],
    *,
    is_local: bool,
    using: str,
) -> dict[str, str | None]:
    """Snapshot then set several GUCs in one sync call (one thread hop from async code).

    If setting one variable fails, the variables already changed are restored
    before the error propagates, so the caller never has to restore a
    half-applied block.

    Args:
        variables: Mapping of GUC name to the value to set.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias.

    Returns:
        Mapping of GUC name to the value read before the change, for
        :func:`restore_gucs`.
    """
    previous = {name: get_guc(name, using=using) for name in variables}
    done: dict[str, str | None] = {}
    try:
        for name, value in variables.items():
            set_guc(name, value, is_local=is_local, using=using)
            done[name] = previous[name]
    except BaseException:
        restore_gucs(done, is_local=is_local, using=using, exc_pending=True)
        raise
    return previous


async def _run_enter_hop(
    enter: Callable[..., dict[str, str | None]],
    variables: Mapping[str, str],
    *,
    is_local: bool,
    using: str,
    undo: Callable[..., object] = restore_gucs,
) -> dict[str, str | None]:
    """Run an enter helper in one thread-sensitive hop, undoing it if cancelled meanwhile.

    asgiref cannot stop a sync function that is already running in its
    thread: when the awaiting task is cancelled, the hop completes anyway,
    its result is discarded and ``CancelledError`` is raised. Without this
    guard the GUCs the hop had just set would stay on the connection with
    nothing left to restore them. The hop is therefore shielded: a
    cancellation waits for the snapshot it returns, restores it in a second
    hop and only then propagates.

    Args:
        enter: ``_enter_gucs`` or a wrapper with the same signature.
        variables: Mapping of GUC name to the value to set.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias.
        undo: Restores the snapshot after a cancellation, called like
            :func:`restore_gucs` (the default).

    Returns:
        The snapshot returned by ``enter``, for :func:`restore_gucs`.
    """
    hop = asyncio.ensure_future(
        sync_to_async(enter, thread_sensitive=True)(variables, is_local=is_local, using=using)
    )
    try:
        return await asyncio.shield(hop)
    except asyncio.CancelledError:
        # The hop keeps running in its thread; wait for it so the change can
        # be undone. A second cancellation here is the documented gap.
        previous = await hop
        await sync_to_async(undo, thread_sensitive=True)(
            previous,
            is_local=is_local,
            using=using,
            exc_pending=True,
        )
        raise


@asynccontextmanager
async def async_rls_context(
    variables: dict[str, str],
    *,
    is_local: bool = False,
    using: str = "default",
) -> AsyncIterator[None]:
    """Async counterpart of :func:`rls_context`, for ``async with``.

    Snapshots and sets the variables in one ``thread_sensitive``
    ``sync_to_async`` hop on entry and restores them in one hop on exit (see
    :func:`django_rls_tenants.rls.guc.aset_guc` for what a hop is and why it
    is not a non-blocking call). Nesting, the restore in both GUC modes and
    the aborted-transaction behaviour are the same as for :func:`rls_context`.

    The restore runs even when the task is cancelled while entering or
    inside the block. Only a second cancellation delivered during the
    restore itself can skip it; wrap long-lived tasks in your own cleanup if
    that matters.

    Args:
        variables: Dict of GUC variable names to values.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped). The hop
            thread must then be inside ``transaction.atomic()``, otherwise the
            ``RuntimeError`` from :func:`set_guc` propagates.
        using: Database alias. Default: ``"default"``.
    """
    previous = await _run_enter_hop(_enter_gucs, variables, is_local=is_local, using=using)
    exc_pending = False
    try:
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        await sync_to_async(restore_gucs, thread_sensitive=True)(
            previous,
            is_local=is_local,
            using=using,
            exc_pending=exc_pending,
        )


@asynccontextmanager
async def async_bypass_flag(
    flag_name: str,
    *,
    is_local: bool = False,
    using: str = "default",
) -> AsyncIterator[None]:
    """Async counterpart of :func:`bypass_flag`, for ``async with``.

    Sets the flag to ``'true'`` in one ``thread_sensitive`` hop and restores
    the previous value in one hop on exit, with the same semantics as
    :func:`async_rls_context`.

    Usage::

        async with async_bypass_flag("rls.is_login_request"):
            user = await User.objects.aget(email=email)

    Args:
        flag_name: GUC variable name (e.g., ``"rls.is_login_request"``).
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.
    """
    async with async_rls_context({flag_name: "true"}, is_local=is_local, using=using):
        yield
