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
from typing import TYPE_CHECKING, Any

from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django_rls_tenants.tenants.types import TenantUser

logger = logging.getLogger("django_rls_tenants")


@contextmanager
def tenant_context(
    tenant_id: int | str,
    *,
    using: str = "default",
) -> Iterator[None]:
    """Set RLS context to a specific tenant. Supports nesting.

    Args:
        tenant_id: The tenant PK to scope queries to.
        using: Database alias. Default: ``"default"``.

    Raises:
        ValueError: If ``tenant_id`` is ``None``.
    """
    if tenant_id is None:
        msg = "tenant_id cannot be None. For admin access, use admin_context() instead."
        raise ValueError(msg)

    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET

    # Save previous state for nesting support
    prev_admin: str | None = None
    prev_tenant: str | None = None
    if not is_local:
        prev_admin = get_guc(conf.GUC_IS_ADMIN, using=using)
        prev_tenant = get_guc(conf.GUC_CURRENT_TENANT, using=using)

    set_guc(conf.GUC_IS_ADMIN, "false", is_local=is_local, using=using)
    set_guc(
        conf.GUC_CURRENT_TENANT,
        str(tenant_id),
        is_local=is_local,
        using=using,
    )
    try:
        yield
    finally:
        if not is_local:
            _restore_guc(conf.GUC_IS_ADMIN, prev_admin, using=using)
            _restore_guc(conf.GUC_CURRENT_TENANT, prev_tenant, using=using)


@contextmanager
def admin_context(
    *,
    using: str = "default",
) -> Iterator[None]:
    """Set RLS context to admin mode. Supports nesting.

    Args:
        using: Database alias. Default: ``"default"``.
    """
    conf = rls_tenants_config
    is_local = conf.USE_LOCAL_SET

    # Save previous state for nesting support
    prev_admin: str | None = None
    prev_tenant: str | None = None
    if not is_local:
        prev_admin = get_guc(conf.GUC_IS_ADMIN, using=using)
        prev_tenant = get_guc(conf.GUC_CURRENT_TENANT, using=using)

    set_guc(conf.GUC_IS_ADMIN, "true", is_local=is_local, using=using)
    set_guc(
        conf.GUC_CURRENT_TENANT,
        "-1",
        is_local=is_local,
        using=using,
    )
    try:
        yield
    finally:
        if not is_local:
            _restore_guc(conf.GUC_IS_ADMIN, prev_admin, using=using)
            _restore_guc(conf.GUC_CURRENT_TENANT, prev_tenant, using=using)


def _restore_guc(
    name: str,
    previous: str | None,
    *,
    using: str = "default",
) -> None:
    """Restore a GUC to its previous value, or clear if none."""
    if previous is not None:
        set_guc(name, previous, using=using)
    else:
        clear_guc(name, using=using)


def _get_arg_from_signature(
    func: Callable[..., Any],
    arg_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Extract a named argument from a call, positional or keyword."""
    sig = inspect.signature(func)
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return bound.arguments.get(arg_name)


def with_rls_context(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that extracts ``as_user`` and sets RLS context.

    Inspects the decorated function's signature to find the parameter
    named by ``RLS_TENANTS["USER_PARAM_NAME"]`` (default: ``"as_user"``).
    Based on the user's ``TenantUser`` protocol, sets either
    ``admin_context`` or ``tenant_context``.

    When ``as_user`` is ``None``, logs a warning and proceeds without
    context (fail-closed: RLS blocks all access).
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        conf = rls_tenants_config
        as_user: TenantUser | None = _get_arg_from_signature(
            func, conf.USER_PARAM_NAME, *args, **kwargs
        )

        if as_user is not None and as_user.is_tenant_admin:
            ctx = admin_context()
        elif as_user is not None:
            tenant_id = as_user.rls_tenant_id
            # tenant_context validates None at runtime; narrow type for mypy
            ctx = tenant_context(tenant_id)  # type: ignore[arg-type]
        else:
            logger.warning(
                "with_rls_context: %s is None, no RLS context set (fail-closed)",
                conf.USER_PARAM_NAME,
            )
            return func(*args, **kwargs)

        with ctx:
            return func(*args, **kwargs)

    return wrapper
