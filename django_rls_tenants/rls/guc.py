"""PostgreSQL GUC (Grand Unified Configuration) variable helpers.

Provides ``set_guc``, ``get_guc``, and ``clear_guc`` for managing session-level
or transaction-local configuration parameters used by RLS policies.
"""

from __future__ import annotations

from django.db import connections


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
        RuntimeError: If ``is_local=True`` outside ``transaction.atomic()``.
    """
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
    """
    conn = connections[using]
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [name])
        result = cursor.fetchone()
        value = result[0] if result else None
        return value if value != "" else None


def clear_guc(name: str, *, using: str = "default") -> None:
    """Clear a GUC variable by setting it to an empty string."""
    set_guc(name, "", using=using)
