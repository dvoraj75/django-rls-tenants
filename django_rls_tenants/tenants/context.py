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
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from asgiref.sync import sync_to_async
from django.db import connections

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.rls.context import _enter_gucs, _run_enter_hop, restore_gucs
from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.errors import HINT_TENANT_ID_NONE, HINT_USER_NO_TENANT
from django_rls_tenants.tenants.state import (
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from django_rls_tenants.tenants.conf import RLSTenantsConfig
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


@contextmanager
def tenant_context(
    tenant_id: int | str,
    *,
    using: str = "default",
) -> Iterator[None]:
    """Set RLS context to a specific tenant. Supports nesting.

    The previous GUC values are saved on entry and restored on exit in both
    GUC modes. With ``USE_LOCAL_SET=True`` the restore is issued with
    ``SET LOCAL`` so an enclosing block keeps its database scope for the
    rest of the transaction (see :func:`django_rls_tenants.rls.context.restore_gucs`
    for the behaviour when the block exits with an aborted transaction).

    Args:
        tenant_id: The tenant PK to scope queries to.
        using: Database alias. Default: ``"default"``.

    Raises:
        NoTenantContextError: If ``tenant_id`` is ``None``.
    """
    if tenant_id is None:
        msg = "tenant_id cannot be None."
        raise NoTenantContextError(msg, hint=HINT_TENANT_ID_NONE)

    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET

    # Save previous state for nesting support (both modes: a SET LOCAL made
    # by an enclosing block is visible here and must be re-issued on exit).
    previous = _snapshot_gucs(conf, using=using)

    active_token = set_rls_context_active(True)
    token = set_current_tenant_id(tenant_id)
    exc_pending = False
    try:
        set_guc(conf.GUC_IS_ADMIN, "false", is_local=is_local, using=using)
        set_guc(
            conf.GUC_CURRENT_TENANT,
            str(tenant_id),
            is_local=is_local,
            using=using,
        )
        logger.debug("tenant_context entered: tenant_id=%s, using=%s", tenant_id, using)
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        reset_current_tenant_id(token)
        reset_rls_context_active(active_token)
        restore_gucs(previous, is_local=is_local, using=using, exc_pending=exc_pending)
        logger.debug("tenant_context exited: tenant_id=%s, using=%s", tenant_id, using)


@contextmanager
def admin_context(
    *,
    using: str = "default",
) -> Iterator[None]:
    """Set RLS context to admin mode. Supports nesting.

    The previous GUC values are saved on entry and restored on exit in both
    GUC modes, so an inner ``admin_context()`` never leaks the admin bypass
    into the enclosing block (see :func:`tenant_context`).

    Args:
        using: Database alias. Default: ``"default"``.
    """
    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET

    # Save previous state for nesting support (both modes).
    previous = _snapshot_gucs(conf, using=using)

    active_token = set_rls_context_active(True)
    token = set_current_tenant_id(None)
    exc_pending = False
    try:
        set_guc(conf.GUC_IS_ADMIN, "true", is_local=is_local, using=using)
        # Clear tenant GUC for admin mode; the admin_bypass clause in the
        # RLS policy handles access independently. Avoids the old "-1"
        # sentinel which could collide with integer PKs or fail UUID casts.
        clear_guc(conf.GUC_CURRENT_TENANT, is_local=is_local, using=using)
        logger.debug("admin_context entered: using=%s", using)
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        reset_current_tenant_id(token)
        reset_rls_context_active(active_token)
        restore_gucs(previous, is_local=is_local, using=using, exc_pending=exc_pending)
        logger.debug("admin_context exited: using=%s", using)


def _snapshot_gucs(conf: RLSTenantsConfig, *, using: str) -> dict[str, str | None]:
    """Read the admin and tenant GUCs so a context block can restore them on exit."""
    return {
        conf.GUC_IS_ADMIN: get_guc(conf.GUC_IS_ADMIN, using=using),
        conf.GUC_CURRENT_TENANT: get_guc(conf.GUC_CURRENT_TENANT, using=using),
    }


def _enter_tenant_gucs(
    variables: dict[str, str],
    *,
    is_local: bool,
    using: str,
) -> dict[str, str | None]:
    """Snapshot + set the admin/tenant GUCs for an async block; one thread hop.

    Runs inside the hop, so ``in_atomic_block`` is read from the connection
    the block's queries will use.

    Raises:
        RLSConfigurationError: If ``is_local`` is set and the hop thread is
            not inside ``transaction.atomic()``.
    """
    if is_local and not connections[using].in_atomic_block:
        msg = (
            "async_tenant_context() and async_admin_context() cannot use "
            "RLS_TENANTS['USE_LOCAL_SET'] = True here: SET LOCAL needs a transaction held "
            "open on the thread that runs the database calls, and a coroutine cannot open "
            "one. Run the block from synchronous code inside transaction.atomic() via "
            "async_to_sync, or set USE_LOCAL_SET=False."
        )
        raise RLSConfigurationError(msg)
    return _enter_gucs(variables, is_local=is_local, using=using)


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

    **USE_LOCAL_SET.** ``SET LOCAL`` needs ``transaction.atomic()`` open on
    the hop thread, which a coroutine cannot open. With ``USE_LOCAL_SET=True``
    the block only works when the coroutine is driven by ``async_to_sync``
    from synchronous code that is inside ``atomic()``; anywhere else (a bare
    event loop, an ASGI request) it raises :class:`RLSConfigurationError`.

    **One context per task.** Sibling tasks of one request (for example
    under ``asyncio.gather``) share the request's database connection, so
    their GUC writes interleave. The ORM auto-scope filter still keeps model
    queries scoped (``ContextVar`` state is per task), but raw SQL,
    ``safe_tenant_sql()`` and M2M through tables rely on the GUCs alone and
    are not protected. Do not run context blocks concurrently in sibling
    tasks that share a connection.

    The restore runs even when the task is cancelled while entering or
    inside the block; only a second cancellation delivered during the
    restore itself can skip it (for requests ``RLSTenantMiddleware`` set a
    context for, its safety net clears the GUCs at the end of the request).

    Args:
        tenant_id: The tenant PK to scope queries to.
        using: Database alias. Default: ``"default"``.

    Raises:
        NoTenantContextError: If ``tenant_id`` is ``None``.
        RLSConfigurationError: If ``USE_LOCAL_SET=True`` and no transaction is
            open on the hop thread.
    """
    if tenant_id is None:
        msg = "tenant_id cannot be None."
        raise NoTenantContextError(msg, hint=HINT_TENANT_ID_NONE)

    conf = rls_tenants_config
    # Read once so entry and exit agree even if settings change mid-block.
    is_local = conf.USE_LOCAL_SET

    # GUCs first, ContextVars second (the sync manager does the reverse):
    # a failed hop then leaves no ContextVar to unwind. The ContextVars are
    # set here in the coroutine, never inside the hop, because a token
    # created in the hop's context copy cannot be reset from here (#58).
    previous = await _run_enter_hop(
        _enter_tenant_gucs,
        {conf.GUC_IS_ADMIN: "false", conf.GUC_CURRENT_TENANT: str(tenant_id)},
        is_local=is_local,
        using=using,
    )
    active_token = set_rls_context_active(True)
    token = set_current_tenant_id(tenant_id)
    logger.debug("async_tenant_context entered: tenant_id=%s, using=%s", tenant_id, using)
    exc_pending = False
    try:
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        reset_current_tenant_id(token)
        reset_rls_context_active(active_token)
        await sync_to_async(restore_gucs, thread_sensitive=True)(
            previous,
            is_local=is_local,
            using=using,
            exc_pending=exc_pending,
        )
        logger.debug("async_tenant_context exited: tenant_id=%s, using=%s", tenant_id, using)


@asynccontextmanager
async def async_admin_context(
    *,
    using: str = "default",
) -> AsyncIterator[None]:
    """Async counterpart of :func:`admin_context`, for ``async with``. Supports nesting.

    Same hop model, ``USE_LOCAL_SET`` constraint and one-context-per-task
    rule as :func:`async_tenant_context`. An inner ``async_admin_context()``
    never leaks the admin bypass into the enclosing block.

    Args:
        using: Database alias. Default: ``"default"``.

    Raises:
        RLSConfigurationError: If ``USE_LOCAL_SET=True`` and no transaction is
            open on the hop thread.
    """
    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET

    # Same mapping as admin_context(): clear_guc() is set_guc(name, "").
    previous = await _run_enter_hop(
        _enter_tenant_gucs,
        {conf.GUC_IS_ADMIN: "true", conf.GUC_CURRENT_TENANT: ""},
        is_local=is_local,
        using=using,
    )
    active_token = set_rls_context_active(True)
    token = set_current_tenant_id(None)
    logger.debug("async_admin_context entered: using=%s", using)
    exc_pending = False
    try:
        yield
    except BaseException:
        exc_pending = True
        raise
    finally:
        reset_current_tenant_id(token)
        reset_rls_context_active(active_token)
        await sync_to_async(restore_gucs, thread_sensitive=True)(
            previous,
            is_local=is_local,
            using=using,
            exc_pending=exc_pending,
        )
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


def with_rls_context(
    func: Callable[_P, _R] | None = None,
    *,
    user_param: str | None = None,
) -> Callable[_P, _R] | Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorator that extracts a user argument and sets RLS context.

    Can be used bare or with an explicit ``user_param``::

        @with_rls_context
        def my_view(request, as_user): ...

        @with_rls_context(user_param="current_user")
        def my_view(request, current_user): ...

    Args:
        func: The function to decorate (when used without parentheses).
        user_param: Override the parameter name to look for. Defaults to
            ``RLS_TENANTS["USER_PARAM_NAME"]`` (default: ``"as_user"``).

    Returns:
        The decorated function with the same signature as the original,
        or a decorator factory when called with keyword arguments.

    When the user argument is ``None``, logs a warning and proceeds
    without context (fail-closed: RLS blocks all access).
    """

    def decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        # Cache signature at decoration time (not per-invocation).
        sig = inspect.signature(fn)
        param_name = user_param if user_param is not None else rls_tenants_config.USER_PARAM_NAME

        if param_name not in sig.parameters:
            logger.warning(
                "with_rls_context: parameter %r not found in signature of %s. "
                "RLS context will never be set (fail-closed). "
                "Use @with_rls_context(user_param='your_param') to specify explicitly.",
                param_name,
                fn.__qualname__,
            )

        @functools.wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            as_user: TenantUser | None = _get_arg_from_signature(sig, param_name, *args, **kwargs)

            if as_user is not None and as_user.is_tenant_admin:
                ctx = admin_context()
            elif as_user is not None:
                tenant_id = as_user.rls_tenant_id
                if tenant_id is None:
                    msg = f"Non-admin user passed to {fn.__qualname__} has rls_tenant_id=None."
                    raise NoTenantContextError(msg, hint=HINT_USER_NO_TENANT)
                ctx = tenant_context(tenant_id)
            else:
                logger.warning(
                    "with_rls_context: %s is None in call to %s, no RLS context set (fail-closed)",
                    param_name,
                    fn.__qualname__,
                )
                return fn(*args, **kwargs)

            with ctx:
                return fn(*args, **kwargs)

        return wrapper

    # Support both @with_rls_context and @with_rls_context(user_param="x")
    if func is not None:
        return decorator(func)
    return decorator
