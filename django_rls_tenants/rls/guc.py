"""PostgreSQL GUC (Grand Unified Configuration) variable helpers.

Provides ``set_guc``, ``get_guc``, ``clear_guc`` and ``restore_guc`` for managing
session-level or transaction-local configuration parameters used by RLS policies,
plus their async counterparts ``aset_guc``, ``aget_guc``, ``aclear_guc`` and
``arestore_guc``. The async variants run the sync function in a thread-sensitive
``sync_to_async`` hop: Django has no asynchronous database cursor, so they are not
non-blocking, but they execute on the thread that owns the task's connection.
"""

from __future__ import annotations

import re

from asgiref.sync import sync_to_async
from django.db import connections

# Valid GUC names: dotted identifiers like "rls.current_tenant" or "myapp.is_admin".
# ``\Z`` (not ``$``) anchors the end: ``$`` also matches just before a trailing
# newline, which would let "rls.x\n" slip through.
_GUC_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*\Z")


def _validate_guc_name(name: str) -> None:
    """Validate a GUC variable name to prevent SQL injection.

    Args:
        name: GUC variable name to validate.

    Raises:
        ValueError: If ``name`` contains invalid characters.
    """
    if not _GUC_NAME_RE.match(name):
        msg = (
            f"Invalid GUC variable name: {name!r}. "
            f"GUC names must match [a-zA-Z_][a-zA-Z0-9_.]* "
            f"(e.g., 'rls.current_tenant')."
        )
        raise ValueError(msg)


def set_guc(
    name: str,
    value: str,
    *,
    is_local: bool = False,
    using: str = "default",
) -> None:
    """Set a PostgreSQL session variable (GUC).

    Args:
        name: Variable name (e.g., ``"rls.current_tenant"``).
        value: Variable value as string.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
            If ``False``, use ``set_config`` (session-scoped, persists until changed).
        using: Database alias. Default: ``"default"``.

    Raises:
        ValueError: If ``name`` contains invalid characters.
        RuntimeError: If ``is_local=True`` outside ``transaction.atomic()``.
    """
    _validate_guc_name(name)
    conn = connections[using]
    if is_local and not conn.in_atomic_block:
        raise RuntimeError(
            f"Cannot use SET LOCAL for '{name}' outside a transaction. "
            f"Wrap your code in transaction.atomic() or use is_local=False."
        )
    with conn.cursor() as cursor:
        if is_local:
            # SET LOCAL is transaction-scoped; auto-clears at commit/rollback.
            # The variable name is developer-controlled, not user input.
            cursor.execute(f"SET LOCAL {name} TO %s", [value])
        else:
            cursor.execute("SELECT set_config(%s, %s, false)", [name, value])


def get_guc(name: str, *, using: str = "default") -> str | None:
    """Get a PostgreSQL session variable value.

    Returns:
        The variable value, or ``None`` if unset or empty.

    Raises:
        ValueError: If ``name`` contains invalid characters.
    """
    _validate_guc_name(name)
    conn = connections[using]
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [name])
        result = cursor.fetchone()
        value = result[0] if result else None
        return value if value != "" else None


def clear_guc(name: str, *, is_local: bool = False, using: str = "default") -> None:
    """Clear a GUC variable by setting it to an empty string.

    Args:
        name: Variable name to clear.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.
    """
    set_guc(name, "", is_local=is_local, using=using)


def restore_guc(
    name: str,
    previous: str | None,
    *,
    is_local: bool = False,
    using: str = "default",
) -> None:
    """Put a GUC back to a value previously read with :func:`get_guc`.

    ``None`` (unset or empty before) clears the variable. ``is_local`` must
    match the scope the value was changed with: a ``SET LOCAL`` change is
    undone with another ``SET LOCAL`` so the outer transaction-local value
    is reinstated, while a session-level change is undone with ``set_config``.

    Args:
        name: Variable name to restore.
        previous: Value returned by ``get_guc`` before the change.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.
    """
    if previous is not None:
        set_guc(name, previous, is_local=is_local, using=using)
    else:
        clear_guc(name, is_local=is_local, using=using)


async def aset_guc(
    name: str,
    value: str,
    *,
    is_local: bool = False,
    using: str = "default",
) -> None:
    """Async counterpart of :func:`set_guc`.

    Runs ``set_guc`` in a ``thread_sensitive`` ``sync_to_async`` hop. Django
    has no asynchronous database cursor, so this is not a non-blocking call:
    it executes on the thread that owns the current task's database
    connection (under ASGI, the request's thread-sensitive thread, shared
    with the async ORM), which is what keeps the GUC on the same connection
    the queries use.

    Args:
        name: Variable name (e.g., ``"rls.current_tenant"``).
        value: Variable value as string.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
            If ``False``, use ``set_config`` (session-scoped, persists until changed).
        using: Database alias. Default: ``"default"``.

    Raises:
        ValueError: If ``name`` contains invalid characters.
        RuntimeError: If ``is_local=True`` and the hop thread is not inside
            ``transaction.atomic()``. A coroutine cannot open that transaction
            itself (``atomic()`` on the event-loop thread raises
            ``SynchronousOnlyOperation``); it has to be open in the synchronous
            code that called the coroutine through ``async_to_sync``.
    """
    await sync_to_async(set_guc, thread_sensitive=True)(
        name,
        value,
        is_local=is_local,
        using=using,
    )


async def aget_guc(name: str, *, using: str = "default") -> str | None:
    """Async counterpart of :func:`get_guc`.

    Runs ``get_guc`` in a ``thread_sensitive`` ``sync_to_async`` hop on the
    thread that owns the current task's database connection (see
    :func:`aset_guc`; this is not a non-blocking call).

    Args:
        name: Variable name (e.g., ``"rls.current_tenant"``).
        using: Database alias. Default: ``"default"``.

    Returns:
        The variable value, or ``None`` if unset or empty.

    Raises:
        ValueError: If ``name`` contains invalid characters.
    """
    return await sync_to_async(get_guc, thread_sensitive=True)(name, using=using)


async def aclear_guc(name: str, *, is_local: bool = False, using: str = "default") -> None:
    """Async counterpart of :func:`clear_guc`.

    Runs ``clear_guc`` in a ``thread_sensitive`` ``sync_to_async`` hop on the
    thread that owns the current task's database connection (see
    :func:`aset_guc`; this is not a non-blocking call).

    Args:
        name: Variable name to clear.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.

    Raises:
        ValueError: If ``name`` contains invalid characters.
        RuntimeError: If ``is_local=True`` and the hop thread is not inside
            ``transaction.atomic()`` (see :func:`aset_guc`).
    """
    await sync_to_async(clear_guc, thread_sensitive=True)(name, is_local=is_local, using=using)


async def arestore_guc(
    name: str,
    previous: str | None,
    *,
    is_local: bool = False,
    using: str = "default",
) -> None:
    """Async counterpart of :func:`restore_guc`.

    Runs ``restore_guc`` in a ``thread_sensitive`` ``sync_to_async`` hop on
    the thread that owns the current task's database connection (see
    :func:`aset_guc`; this is not a non-blocking call).

    Args:
        name: Variable name to restore.
        previous: Value returned by ``get_guc`` / ``aget_guc`` before the change.
        is_local: If ``True``, use ``SET LOCAL`` (transaction-scoped).
        using: Database alias. Default: ``"default"``.

    Raises:
        ValueError: If ``name`` contains invalid characters.
        RuntimeError: If ``is_local=True`` and the hop thread is not inside
            ``transaction.atomic()`` (see :func:`aset_guc`).
    """
    await sync_to_async(restore_guc, thread_sensitive=True)(
        name,
        previous,
        is_local=is_local,
        using=using,
    )
