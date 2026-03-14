"""PostgreSQL GUC (Grand Unified Configuration) variable helpers.

Provides ``set_guc``, ``get_guc``, and ``clear_guc`` for managing session-level
or transaction-local configuration parameters used by RLS policies.
"""

from __future__ import annotations

import re

from django.db import connections

# Valid GUC names: dotted identifiers like "rls.current_tenant" or "myapp.is_admin".
_GUC_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


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


def clear_guc(name: str, *, using: str = "default") -> None:
    """Clear a GUC variable by setting it to an empty string."""
    set_guc(name, "", using=using)
