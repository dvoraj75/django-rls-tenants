"""Generic RLS context managers.

Provides ``rls_context`` for setting/clearing arbitrary GUC variables,
and ``bypass_flag`` for toggling boolean bypass flags within a
transaction-scoped context. Both support save/restore nesting.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def rls_context(
    variables: dict[str, str],
    *,
    is_local: bool = False,
    using: str = "default",
) -> Iterator[None]:
    """Set multiple GUC variables for the duration of a block.

    Saves and restores previous values on exit (supports nesting).

    Args:
        variables: Dict of GUC variable names to values.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.
    """
    previous_values: dict[str, str | None] = {}
    if not is_local:
        for name in variables:
            previous_values[name] = get_guc(name, using=using)

    for name, value in variables.items():
        set_guc(name, value, is_local=is_local, using=using)
    try:
        yield
    finally:
        if not is_local:  # SET LOCAL auto-clears; session-level needs manual restore
            for name, prev in previous_values.items():
                if prev is not None:
                    set_guc(name, prev, using=using)
                else:
                    clear_guc(name, using=using)


@contextmanager
def bypass_flag(
    flag_name: str,
    *,
    is_local: bool = False,
    using: str = "default",
) -> Iterator[None]:
    """Temporarily set a GUC bypass flag to ``'true'``.

    Saves and restores previous value on exit (supports nesting).

    Usage::

        with bypass_flag("rls.is_login_request"):
            user = User.objects.get(email=email)
    """
    previous: str | None = None
    if not is_local:
        previous = get_guc(flag_name, using=using)

    set_guc(flag_name, "true", is_local=is_local, using=using)
    try:
        yield
    finally:
        if not is_local:
            if previous is not None:
                set_guc(flag_name, previous, using=using)
            else:
                clear_guc(flag_name, using=using)
