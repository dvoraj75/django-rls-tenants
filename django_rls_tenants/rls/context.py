"""Generic RLS context managers.

Provides ``rls_context`` for setting/clearing arbitrary GUC variables,
and ``bypass_flag`` for toggling boolean bypass flags within a
transaction-scoped context. Both support save/restore nesting in both
GUC modes (``set_config`` and ``SET LOCAL``).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import DatabaseError

from django_rls_tenants.rls.guc import get_guc, restore_guc, set_guc

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = logging.getLogger("django_rls_tenants")


def restore_gucs(
    previous: Mapping[str, str | None],
    *,
    is_local: bool = False,
    using: str = "default",
    exc_pending: bool = False,
) -> None:
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

    Args:
        previous: Mapping of GUC name to the value read before the block.
        is_local: Scope the block used; ``True`` re-issues ``SET LOCAL``.
        using: Database alias. Default: ``"default"``.
        exc_pending: ``True`` when called while an exception is propagating.
    """
    for name, prev in previous.items():
        try:
            restore_guc(name, prev, is_local=is_local, using=using)
        except DatabaseError:
            if not exc_pending:
                raise
            logger.debug(
                "Skipping GUC restore for %s: the transaction is already aborted and "
                "will be rolled back; the original exception is propagated instead.",
                name,
                exc_info=True,
            )


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
