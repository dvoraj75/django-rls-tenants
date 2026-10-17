"""Tenant-aware RLS context managers and decorator.

Provides ``tenant_context``, ``admin_context`` for scoping database
access, their ``async with`` counterparts ``async_tenant_context`` and
``async_admin_context``, and ``with_rls_context`` for automatically
extracting user context from function arguments.
"""

from __future__ import annotations

import functools
import inspect
import logging
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, NoReturn, cast, overload

from asgiref.sync import sync_to_async
from django.db import connections

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.rls.context import _enter_gucs, _run_enter_hop, restore_gucs
from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.affinity import (
    UNKNOWN,
    _forget,
    apply_active_context,
    settle_other_aliases,
    suspect_sessions,
    writing,
)
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.errors import HINT_TENANT_ID_NONE, HINT_USER_NO_TENANT
from django_rls_tenants.tenants.state import (
    ADMIN_GUCS,
    ContextGucs,
    _current_live,
    _get_context_gucs,
    _reset_context_gucs,
    _set_context_gucs,
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping

    from django_rls_tenants.tenants.conf import RLSTenantsConfig
    from django_rls_tenants.tenants.state import _Live
    from django_rls_tenants.tenants.types import TenantUser

logger = logging.getLogger("django_rls_tenants")


def _resolve_user_guc_vars(
    user: TenantUser,
    conf: RLSTenantsConfig | None = None,
) -> dict[str, str]:
    """Map a ``TenantUser`` to GUC variable name-value pairs.

    Centralises the "if admin then X, else Y" mapping so that
    middleware, managers, and context managers all use the same logic.

    For admin users, ``GUC_CURRENT_TENANT`` is cleared (empty string)
    instead of a magic sentinel. The ``admin_bypass`` clause in the
    RLS policy handles access independently.

    Args:
        user: User satisfying the ``TenantUser`` protocol.
        conf: Config instance. Defaults to the module-level singleton.

    Returns:
        Dict mapping GUC variable names to their string values.
    """
    if conf is None:
        conf = rls_tenants_config
    if user.is_tenant_admin:
        return {
            conf.GUC_IS_ADMIN: "true",
            conf.GUC_CURRENT_TENANT: "",
        }
    tenant_id = user.rls_tenant_id
    if tenant_id is None:
        msg = f"Non-admin user has rls_tenant_id=None (user type: {type(user).__name__})."
        raise NoTenantContextError(msg, hint=HINT_USER_NO_TENANT)
    return {
        conf.GUC_IS_ADMIN: "false",
        conf.GUC_CURRENT_TENANT: str(tenant_id),
    }


def _context_gucs_of(variables: Mapping[str, str], conf: RLSTenantsConfig) -> ContextGucs:
    """Return the ``ContextGucs`` of a ``_resolve_user_guc_vars()`` mapping.

    The form the affinity guard compares and applies (see ``state._get_context_gucs()``).
    """
    return ContextGucs(variables[conf.GUC_IS_ADMIN], variables[conf.GUC_CURRENT_TENANT])


@contextmanager
def _rls_state(tenant_id: int | str | None, gucs: ContextGucs) -> Iterator[None]:
    """Set the tenant ``ContextVar`` values of an RLS context; reset them on exit.

    The auto-scope tenant, the strict-mode flag and the GUC values the
    affinity guard applies. They are reset newest first, and a failing reset
    does not skip the others.

    Args:
        tenant_id: The auto-scope tenant, ``None`` for an admin context.
        gucs: The GUC values the context sets.
    """
    active_token = set_rls_context_active(True)
    tenant_token = set_current_tenant_id(tenant_id)
    entered = _set_context_gucs(gucs)
    try:
        yield
    finally:
        try:
            _reset_context_gucs(entered)
        finally:
            try:
                reset_current_tenant_id(tenant_token)
            finally:
                reset_rls_context_active(active_token)


def _writes_gucs(using: str, *, is_local: bool) -> bool:
    """Whether a context block entered on ``using`` writes the admin and tenant GUCs itself.

    With session GUCs it always does. With ``USE_LOCAL_SET=True``, on an
    alias in ``RLS_TENANTS["DATABASES"]``, it never does, inside
    ``transaction.atomic()`` or not: the block only sets the ``ContextVar``
    values, and the GUC affinity guard applies the context transaction-locally
    before the first statement that needs it, in one ``set_config()``
    statement, and again only when the context changes (#96, #137). On any
    other alias the block writes ``SET LOCAL`` itself, which fails outside
    ``transaction.atomic()``.

    Args:
        using: Database alias the block is entered on.
        is_local: Scope the block uses (``USE_LOCAL_SET``, read once).
    """
    return not is_local or using not in rls_tenants_config.DATABASES


def _refuse_tenant_id_none(origin: audit.Origin) -> NoReturn:
    """Refuse ``tenant_context(None)``: log ``context.denied``, raise ``NoTenantContextError``.

    Args:
        origin: The block's audit attribution (``audit.current_origin()``).
    """
    if origin.source is not None:
        audit.denied(origin.source, "tenant_id_none", user_id=origin.user_id)
    msg = "tenant_id cannot be None."
    raise NoTenantContextError(msg, hint=HINT_TENANT_ID_NONE)


@contextmanager
def tenant_context(
    tenant_id: int | str,
    *,
    using: str = "default",
) -> Iterator[None]:
    """Set RLS context to a specific tenant. Supports nesting.

    With session GUCs the previous values are saved on entry and restored on
    exit (see :func:`django_rls_tenants.rls.context.restore_gucs` for the
    behaviour when the block exits with an aborted transaction).
    With ``USE_LOCAL_SET=True`` the block writes nothing on entry: the GUC
    affinity guard sets the context's values transaction-locally before the
    first statement that needs them, inside ``transaction.atomic()`` or not,
    giving a statement run in autocommit a transaction of its own (see "GUC
    affinity guard" in the architecture guide). On exit inside a transaction
    the guard puts the enclosing context's values back, or clears them, right
    away. On an alias outside ``RLS_TENANTS["DATABASES"]`` that mode writes and
    restores with ``SET LOCAL``, so it needs ``transaction.atomic()``.

    Args:
        tenant_id: The tenant PK to scope queries to.
        using: Database alias the GUCs are written to on entry and restored on
            exit. Default: ``"default"``. Every alias in
            ``RLS_TENANTS["DATABASES"]`` follows the context too (the GUC
            affinity guard applies it before each statement there).

    Raises:
        NoTenantContextError: If ``tenant_id`` is ``None``.
        RuntimeError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and its connection is not inside
            ``transaction.atomic()``.
    """
    origin = audit.current_origin()
    if tenant_id is None:
        _refuse_tenant_id_none(origin)

    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET
    writes = _writes_gucs(using, is_local=is_local)

    # Save previous state for nesting support (a SET LOCAL made by an
    # enclosing block on an unguarded alias is visible here and must be
    # re-issued on exit).
    previous = _snapshot_gucs(conf, using=using) if writes else None
    session = _session_of(using)
    enclosing = _current_live()

    gucs = ContextGucs.for_tenant(tenant_id)
    exc_pending = False
    entered = False
    try:
        with _rls_state(tenant_id, gucs):
            if writes:
                with writing(using, gucs, local=is_local):
                    set_guc(conf.GUC_IS_ADMIN, gucs.is_admin, is_local=is_local, using=using)
                    set_guc(
                        conf.GUC_CURRENT_TENANT,
                        gucs.current_tenant,
                        is_local=is_local,
                        using=using,
                    )
            logger.debug("tenant_context entered: tenant_id=%s, using=%s", tenant_id, using)
            audit.context_event(audit.CONTEXT_ENTERED, origin, tenant_id=tenant_id, using=using)
            entered = True
            yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        try:
            _restore_tenant_gucs(
                previous,
                is_local=is_local,
                using=using,
                exc_pending=exc_pending,
                enclosing=enclosing,
                session=session,
            )
        finally:
            if entered:
                audit.context_event(audit.CONTEXT_EXITED, origin, tenant_id=tenant_id, using=using)
        logger.debug("tenant_context exited: tenant_id=%s, using=%s", tenant_id, using)


@contextmanager
def admin_context(
    *,
    using: str = "default",
) -> Iterator[None]:
    """Set RLS context to admin mode. Supports nesting.

    The enclosing block's values come back on exit in both GUC modes, so an
    inner ``admin_context()`` never leaks the admin bypass into the enclosing
    block (see :func:`tenant_context`, also for ``USE_LOCAL_SET=True``).

    Args:
        using: Database alias the GUCs are written to on entry and restored on
            exit. Default: ``"default"``. Every alias in
            ``RLS_TENANTS["DATABASES"]`` gets the admin bypass too, even when
            ``using`` names an alias outside that list.

    Raises:
        RuntimeError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and its connection is not inside
            ``transaction.atomic()``.
    """
    origin = audit.current_origin()
    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET
    writes = _writes_gucs(using, is_local=is_local)

    # Save previous state for nesting support.
    previous = _snapshot_gucs(conf, using=using) if writes else None
    session = _session_of(using)
    enclosing = _current_live()

    exc_pending = False
    entered = False
    try:
        with _rls_state(None, ADMIN_GUCS):
            if writes:
                with writing(using, ADMIN_GUCS, local=is_local):
                    set_guc(
                        conf.GUC_IS_ADMIN,
                        ADMIN_GUCS.is_admin,
                        is_local=is_local,
                        using=using,
                    )
                    # Clear tenant GUC for admin mode; the admin_bypass clause in the
                    # RLS policy handles access independently. Avoids the old "-1"
                    # sentinel which could collide with integer PKs or fail UUID casts.
                    clear_guc(conf.GUC_CURRENT_TENANT, is_local=is_local, using=using)
            logger.debug("admin_context entered: using=%s", using)
            audit.context_event(audit.CONTEXT_ENTERED, origin, tenant_id=None, using=using)
            entered = True
            yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        try:
            _restore_tenant_gucs(
                previous,
                is_local=is_local,
                using=using,
                exc_pending=exc_pending,
                enclosing=enclosing,
                session=session,
            )
        finally:
            if entered:
                audit.context_event(audit.CONTEXT_EXITED, origin, tenant_id=None, using=using)
        logger.debug("admin_context exited: using=%s", using)


def _session_of(using: str) -> object:
    """Return the DB-API connection (the database session) of ``using`` on this thread."""
    return connections[using].connection


def _snapshot_gucs(conf: RLSTenantsConfig, *, using: str) -> dict[str, str | None]:
    """Read the admin and tenant GUCs so a context block can restore them on exit.

    Called before the block's ``ContextVar`` values are set: the GUC affinity
    guard brings the connection to the enclosing context's values before the
    first read, so the snapshot holds those values (see :func:`_restore_tenant_gucs`).
    """
    return {
        conf.GUC_IS_ADMIN: get_guc(conf.GUC_IS_ADMIN, using=using),
        conf.GUC_CURRENT_TENANT: get_guc(conf.GUC_CURRENT_TENANT, using=using),
    }


def _restore_tenant_gucs(
    previous: Mapping[str, str | None] | None,
    *,
    is_local: bool,
    using: str,
    exc_pending: bool,
    enclosing: _Live | None,
    session: object,
) -> None:
    """Restore a context block's snapshot of the admin and tenant GUCs, on exit.

    Called after the block's ``ContextVar`` values have been reset. The
    snapshot holds the enclosing context's values (see :func:`_snapshot_gucs`),
    so they are what the GUC affinity guard records for the connection, unless
    a failed restore was skipped (see
    :func:`django_rls_tenants.rls.context.restore_gucs`): the values are then
    unknown, and the guard applies the enclosing context before the next
    statement. If the enclosing context has ended meanwhile (the block ran in
    a task or thread that outlived it), its values do not come back: both GUCs
    are cleared. The other configured aliases, on which the guard may have
    applied the block's context, are brought back to the enclosing one too.
    A block that exits in another ``Context`` than the one it entered in (a
    generator closed or collected there) finds another context current: its
    snapshot is the state of the session it entered on, not that context's.
    It is restored only on that session (a ``SET LOCAL`` would otherwise
    outlive the block there), and the values are left unknown, so the guard
    applies the current context before the next statement.

    A block that wrote nothing on entry (``USE_LOCAL_SET=True`` on an alias
    in ``RLS_TENANTS["DATABASES"]``, see :func:`_writes_gucs`) has nothing to
    restore: the guard set its values transaction-locally, so those set in
    the block's own transactions have ended with them. A transaction still
    open (``transaction.atomic()``, ``AUTOCOMMIT=False``) may hold them, so
    every configured alias is settled: one ``set_config()`` statement each
    where the block's values are still in force, which puts the enclosing
    context's values back or clears them. With
    ``USE_LOCAL_SET=True`` the values the sessions hold for themselves then
    count as unknown (``affinity.suspect_sessions()``): code in the block may
    have written them for the session.

    Args:
        previous: The snapshot taken on entry, or ``None`` for a block that
            wrote nothing.
        is_local: Scope the block used.
        using: Database alias.
        exc_pending: ``True`` when called while an exception is propagating.
        enclosing: The enclosing context's holder when the block was entered,
            from ``state._current_live()``.
        session: The DB-API connection of ``using`` the block entered on.
    """
    try:
        if previous is None:
            settle_other_aliases(None)
        else:
            _restore_snapshot(
                previous,
                is_local=is_local,
                using=using,
                exc_pending=exc_pending,
                enclosing=enclosing,
                session=session,
            )
    finally:
        if is_local:
            suspect_sessions()


def _restore_snapshot(
    previous: Mapping[str, str | None],
    *,
    is_local: bool,
    using: str,
    exc_pending: bool,
    enclosing: _Live | None,
    session: object,
) -> None:
    """Restore a block's snapshot on ``using`` and settle the other aliases.

    The part of :func:`_restore_tenant_gucs` for a block that wrote the GUCs
    on entry; it takes the same arguments.
    """
    if enclosing is not None and not enclosing.alive:
        conf = rls_tenants_config
        previous = dict.fromkeys((conf.GUC_IS_ADMIN, conf.GUC_CURRENT_TENANT))
    if _current_live() is not enclosing:
        # The block exits in a Context other than the one it entered in (a
        # generator closed there): its snapshot is not this context's.
        logger.debug("RLS context block exited in another context; GUC values left unknown")
        if session is not None and _session_of(using) is session:
            with writing(using, UNKNOWN, local=is_local):
                restore_gucs(previous, is_local=is_local, using=using, exc_pending=exc_pending)
        else:
            _forget(using)
        settle_other_aliases(using)
        return
    with writing(using, _get_context_gucs(), local=is_local):
        restored = restore_gucs(previous, is_local=is_local, using=using, exc_pending=exc_pending)
    if not restored:
        _forget(using)
    settle_other_aliases(using)


def _enter_tenant_gucs(
    variables: dict[str, str],
    *,
    is_local: bool,
    using: str,
    entered_on: list[object],
) -> dict[str, str | None] | None:
    """Snapshot + set the admin/tenant GUCs for an async block; one thread hop.

    Runs inside the hop, so ``in_atomic_block`` is read from the connection
    the block's queries will use. The GUC affinity guard first brings the
    connection to the enclosing context's values (the hop holds a copy of the
    coroutine's ``ContextVar`` values, which the block has not changed yet),
    so the snapshot holds those values, as in :func:`_snapshot_gucs`. With
    ``USE_LOCAL_SET=True`` on an alias in ``RLS_TENANTS["DATABASES"]`` it
    writes nothing (see :func:`_writes_gucs`).

    Args:
        variables: Mapping of GUC name to the value to set.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias.
        entered_on: Receives the DB-API connection the block enters on, for
            :func:`_restore_tenant_gucs`.

    Returns:
        The snapshot, for :func:`_restore_tenant_gucs`, or ``None`` if
        nothing was written.

    Raises:
        RLSConfigurationError: If ``is_local`` is set, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and the hop thread is not inside
            ``transaction.atomic()``.
    """
    connection = connections[using]
    if not _writes_gucs(using, is_local=is_local):
        return None
    if is_local and not connection.in_atomic_block:
        msg = (
            f"async_tenant_context() and async_admin_context() cannot use "
            f"RLS_TENANTS['USE_LOCAL_SET'] = True on alias {using!r} here: it is not in "
            f"RLS_TENANTS['DATABASES'], so the GUC affinity guard does not apply the "
            f"context there, and SET LOCAL needs a transaction held open on the thread "
            f"that runs the database calls, which a coroutine cannot open. Add {using!r} "
            f"to RLS_TENANTS['DATABASES'], or run the block from synchronous code inside "
            f"transaction.atomic() via async_to_sync."
        )
        raise RLSConfigurationError(msg)
    apply_active_context(connection)
    with writing(connection, _context_gucs_of(variables, rls_tenants_config), local=is_local):
        previous = _enter_gucs(variables, is_local=is_local, using=using)
    entered_on.append(connection.connection)
    return previous


@asynccontextmanager
async def async_tenant_context(
    tenant_id: int | str,
    *,
    using: str = "default",
) -> AsyncIterator[None]:
    """Async counterpart of :func:`tenant_context`, for ``async with``. Supports nesting.

    Usage::

        async with async_tenant_context(tenant.pk):
            count = await Order.objects.acount()

    The GUCs are snapshotted and set in one ``thread_sensitive``
    ``sync_to_async`` hop on entry and restored in one hop on exit. Django
    has no asynchronous database cursor, so this is not a non-blocking call;
    the hop runs on the thread that owns the task's connection (under ASGI,
    the request's thread, shared with the async ORM). The tenant state
    ``ContextVar`` values are set and reset in the coroutine itself, so the
    ORM auto-scope filter and ``STRICT_MODE`` see them in async code. Nesting
    (with sync or async blocks) and the restore in both GUC modes behave as
    in :func:`tenant_context`.

    **USE_LOCAL_SET.** With ``USE_LOCAL_SET=True`` the block writes nothing:
    the GUC affinity guard sets its values transaction-locally before the
    statements that need them (see :func:`tenant_context`). A coroutine
    cannot open ``transaction.atomic()``, so the hop thread is usually in
    autocommit, and each statement gets a transaction of its own; when the
    coroutine is driven by ``async_to_sync`` from synchronous code inside
    ``atomic()``, the guard sets the values in that transaction. On an alias
    outside ``RLS_TENANTS["DATABASES"]``, which the guard leaves alone, the
    block writes ``SET LOCAL`` itself: it needs that transaction and raises
    :class:`RLSConfigurationError` without one.

    **Concurrent tasks.** Sibling tasks of one request (for example under
    ``asyncio.gather``) share the request's database connection, but each
    has its own ``ContextVar`` state. The GUC affinity guard applies the
    context of the task that runs a statement before that statement, so
    blocks for different tenants can run concurrently, raw SQL,
    ``safe_tenant_sql()`` and M2M through tables included. It does not cover
    bypass flags and other GUCs set with ``async_bypass_flag()`` /
    ``async_rls_context()`` (sibling tasks see them), or SQL that bypasses
    Django's cursor. Work that outlives the block (a task it started without
    awaiting it) runs without its context.

    The restore runs even when the task is cancelled while entering or
    inside the block; only a second cancellation delivered during the
    restore itself can skip it (for requests ``RLSTenantMiddleware`` set a
    context for, its safety net clears the GUCs at the end of the request).

    Args:
        tenant_id: The tenant PK to scope queries to.
        using: Database alias the GUCs are written to first, as in
            :func:`tenant_context`. Default: ``"default"``.

    Raises:
        NoTenantContextError: If ``tenant_id`` is ``None``.
        RLSConfigurationError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and no transaction is open on the hop
            thread.
    """
    origin = audit.current_origin()
    if tenant_id is None:
        _refuse_tenant_id_none(origin)

    conf = rls_tenants_config
    # Read once so entry and exit agree even if settings change mid-block.
    is_local = conf.USE_LOCAL_SET

    # GUCs first, ContextVars second (the sync manager does the reverse):
    # a failed hop then leaves no ContextVar to unwind. The ContextVars are
    # set here in the coroutine, never inside the hop, because a token
    # created in the hop's context copy cannot be reset from here (#58).
    gucs = ContextGucs.for_tenant(tenant_id)
    enclosing = _current_live()
    entered_on: list[object] = []
    previous = await _run_enter_hop(
        functools.partial(_enter_tenant_gucs, entered_on=entered_on),
        {conf.GUC_IS_ADMIN: gucs.is_admin, conf.GUC_CURRENT_TENANT: gucs.current_tenant},
        is_local=is_local,
        using=using,
        # Runs in the Context the block enters in, so the session is not needed.
        undo=functools.partial(_restore_tenant_gucs, enclosing=enclosing, session=None),
    )
    exc_pending = False
    entered = False
    try:
        with _rls_state(tenant_id, gucs):
            logger.debug(
                "async_tenant_context entered: tenant_id=%s, using=%s",
                tenant_id,
                using,
            )
            audit.context_event(audit.CONTEXT_ENTERED, origin, tenant_id=tenant_id, using=using)
            entered = True
            yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        try:
            await sync_to_async(_restore_tenant_gucs, thread_sensitive=True)(
                previous,
                is_local=is_local,
                using=using,
                exc_pending=exc_pending,
                enclosing=enclosing,
                session=next(iter(entered_on), None),
            )
        finally:
            if entered:
                audit.context_event(audit.CONTEXT_EXITED, origin, tenant_id=tenant_id, using=using)
        logger.debug("async_tenant_context exited: tenant_id=%s, using=%s", tenant_id, using)


@asynccontextmanager
async def async_admin_context(
    *,
    using: str = "default",
) -> AsyncIterator[None]:
    """Async counterpart of :func:`admin_context`, for ``async with``. Supports nesting.

    Same hop model, ``USE_LOCAL_SET`` behaviour and concurrent-task
    behaviour as :func:`async_tenant_context`. An inner
    ``async_admin_context()`` never leaks the admin bypass into the
    enclosing block.

    Args:
        using: Database alias the GUCs are written to first, as in
            :func:`admin_context`. Default: ``"default"``.

    Raises:
        RLSConfigurationError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and no transaction is open on the hop
            thread.
    """
    origin = audit.current_origin()
    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET

    # Same mapping as admin_context(): clear_guc() is set_guc(name, "").
    enclosing = _current_live()
    entered_on: list[object] = []
    previous = await _run_enter_hop(
        functools.partial(_enter_tenant_gucs, entered_on=entered_on),
        {conf.GUC_IS_ADMIN: ADMIN_GUCS.is_admin, conf.GUC_CURRENT_TENANT: ""},
        is_local=is_local,
        using=using,
        # Runs in the Context the block enters in, so the session is not needed.
        undo=functools.partial(_restore_tenant_gucs, enclosing=enclosing, session=None),
    )
    exc_pending = False
    entered = False
    try:
        with _rls_state(None, ADMIN_GUCS):
            logger.debug("async_admin_context entered: using=%s", using)
            audit.context_event(audit.CONTEXT_ENTERED, origin, tenant_id=None, using=using)
            entered = True
            yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        try:
            await sync_to_async(_restore_tenant_gucs, thread_sensitive=True)(
                previous,
                is_local=is_local,
                using=using,
                exc_pending=exc_pending,
                enclosing=enclosing,
                session=next(iter(entered_on), None),
            )
        finally:
            if entered:
                audit.context_event(audit.CONTEXT_EXITED, origin, tenant_id=None, using=using)
        logger.debug("async_admin_context exited: using=%s", using)


def _get_arg_from_signature(
    sig: inspect.Signature,
    arg_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Extract a named argument from a call using a pre-computed signature.

    Args:
        sig: Pre-computed ``inspect.Signature`` of the target function.
        arg_name: Name of the parameter to extract.
        *args: Positional arguments from the call.
        **kwargs: Keyword arguments from the call.

    Returns:
        The value of the named argument, or ``None`` if not found.
    """
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return bound.arguments.get(arg_name)


def _decorated_call_tenant_id(as_user: TenantUser, qualname: str) -> int | str | None:
    """Return the tenant a ``@with_rls_context`` call is scoped to, or ``None`` for an admin.

    Args:
        as_user: The user argument of the call (never ``None`` here).
        qualname: Name of the decorated function, for the error message.

    Returns:
        The user's ``rls_tenant_id``, or ``None`` when ``is_tenant_admin`` is true.

    Raises:
        NoTenantContextError: If a non-admin user has ``rls_tenant_id=None``.
    """
    if as_user.is_tenant_admin:
        return None
    tenant_id = as_user.rls_tenant_id
    if tenant_id is None:
        audit.denied("decorator", "user_without_tenant", user_id=audit.user_id_of(as_user))
        msg = f"Non-admin user passed to {qualname} has rls_tenant_id=None."
        raise NoTenantContextError(msg, hint=HINT_USER_NO_TENANT)
    return tenant_id


def _reject_coroutine[T](result: T, qualname: str) -> T:
    """Fail a sync ``@with_rls_context`` call whose function returned a coroutine.

    The sync wrapper has already left the RLS context by the time anyone
    awaits that coroutine, so its body would run under the caller's context
    instead. This happens when ``inspect.iscoroutinefunction()`` cannot see
    that the function is async (an object with ``async def __call__``, or an
    ``async def`` behind a decorator that hides it).

    Args:
        result: The value the decorated function returned.
        qualname: Name of the decorated function, for the error message.

    Returns:
        ``result`` unchanged when it is not a coroutine.

    Raises:
        TypeError: If ``result`` is a coroutine. It is closed first, so
            Python does not warn that it was never awaited.
    """
    if inspect.iscoroutine(result):
        result.close()
        msg = (
            f"@with_rls_context: {qualname} returned a coroutine, but it was decorated as a "
            "synchronous function, so the coroutine would run after the decorator has "
            "returned, outside the RLS context it manages. Decorate the async def function "
            "itself (directly, or under decorators that keep it a coroutine function), or use "
            "async_tenant_context() / async_admin_context() inside it."
        )
        raise TypeError(msg)
    return result


@overload
def with_rls_context[**P, R](
    func: Callable[P, R],
    *,
    user_param: str | None = None,
) -> Callable[P, R]: ...


@overload
def with_rls_context[**P, R](
    func: None = None,
    *,
    user_param: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def with_rls_context[**P, R](
    func: Callable[P, R] | None = None,
    *,
    user_param: str | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that extracts a user argument and sets RLS context.

    Can be used bare or with an explicit ``user_param``::

        @with_rls_context
        def my_view(request, as_user): ...

        @with_rls_context(user_param="current_user")
        def my_view(request, current_user): ...

    ``async def`` functions are supported: the decorator detects them with
    :func:`inspect.iscoroutinefunction` and returns an ``async def`` wrapper
    that enters :func:`async_tenant_context` / :func:`async_admin_context`
    around the awaited body. The wrapper is itself a coroutine function, so
    Django and other async-aware decorators treat it as async::

        @with_rls_context
        async def order_count(as_user):
            return await Order.objects.acount()

    In the async wrapper the user argument is read (``is_tenant_admin``,
    ``rls_tenant_id``) in the coroutine, when the call is awaited, so those
    properties must not query the database; pass a loaded user (in an async
    view, ``await request.auser()``, not ``request.user``).

    Args:
        func: The function to decorate (when used without parentheses).
        user_param: Override the parameter name to look for. Defaults to
            ``RLS_TENANTS["USER_PARAM_NAME"]`` (default: ``"as_user"``).

    Returns:
        The decorated function with the same signature as the original,
        or a decorator factory when called with keyword arguments.

    Raises:
        TypeError: At decoration time, if the function is a generator or an
            async generator function (its body runs while the caller
            iterates, after the context has been exited). At call time, if a
            function decorated as synchronous returns a coroutine.
        NoTenantContextError: At call time (when awaited, for ``async def``),
            if a non-admin user has ``rls_tenant_id=None``.

    When the user argument is ``None``, logs a warning and proceeds
    without context (fail-closed: RLS blocks all access).
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        # Callable objects have no __qualname__ of their own.
        qualname: str = getattr(fn, "__qualname__", type(fn).__qualname__)

        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            msg = (
                f"@with_rls_context cannot decorate the generator function {qualname}: its "
                "body runs while the caller iterates, after the decorator's RLS context has "
                "been exited. Enter the context inside the generator instead (tenant_context() "
                "/ admin_context(), or async_tenant_context() / async_admin_context() in an "
                "async generator)."
            )
            raise TypeError(msg)

        # Cache signature at decoration time (not per-invocation).
        sig = inspect.signature(fn)
        param_name = user_param if user_param is not None else rls_tenants_config.USER_PARAM_NAME

        if param_name not in sig.parameters:
            logger.warning(
                "with_rls_context: parameter %r not found in signature of %s. "
                "RLS context will never be set (fail-closed). "
                "Use @with_rls_context(user_param='your_param') to specify explicitly.",
                param_name,
                qualname,
            )

        def user_of(*args: P.args, **kwargs: P.kwargs) -> TenantUser | None:
            """Read the user argument of one call; log a warning when it is ``None``."""
            as_user: TenantUser | None = _get_arg_from_signature(sig, param_name, *args, **kwargs)
            if as_user is None:
                logger.warning(
                    "with_rls_context: %s is None in call to %s, no RLS context set (fail-closed)",
                    param_name,
                    qualname,
                )
            return as_user

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                as_user = user_of(*args, **kwargs)
                if as_user is None:
                    return await fn(*args, **kwargs)
                tenant_id = _decorated_call_tenant_id(as_user, qualname)
                ctx = (
                    async_admin_context() if tenant_id is None else async_tenant_context(tenant_id)
                )
                async with audit.async_attributed(ctx, source="decorator", user=as_user):
                    return await fn(*args, **kwargs)

            # mypy cannot express "R is a coroutine" for the async branch.
            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            as_user = user_of(*args, **kwargs)
            if as_user is None:
                return _reject_coroutine(fn(*args, **kwargs), qualname)
            tenant_id = _decorated_call_tenant_id(as_user, qualname)
            ctx = admin_context() if tenant_id is None else tenant_context(tenant_id)
            with audit.attributed(ctx, source="decorator", user=as_user):
                return _reject_coroutine(fn(*args, **kwargs), qualname)

        return wrapper

    # Support both @with_rls_context and @with_rls_context(user_param="x")
    if func is not None:
        return decorator(func)
    return decorator
