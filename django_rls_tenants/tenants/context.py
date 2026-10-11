"""Tenant-aware RLS context managers and decorator.

Provides ``tenant_context``, ``admin_context`` for scoping database
access, and ``with_rls_context`` for automatically extracting user
context from function arguments.
"""

from __future__ import annotations

import functools
import inspect
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.context import restore_gucs
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
    from collections.abc import Callable, Iterator

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
