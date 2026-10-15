"""Read the RLS GUCs a database session holds, without going through the GUC affinity guard."""

from __future__ import annotations

from django.db import DEFAULT_DB_ALIAS, connections


def session_gucs(conn=DEFAULT_DB_ALIAS):
    """Return ``(current_tenant, is_admin)`` as the session holds them; empty reads as ``None``.

    Read with a driver cursor: the guard only sees statements that run through
    Django's cursor, so it does not apply the active context first, and the
    result shows what the library (or anything else) left on the session.

    Args:
        conn: A connection, or the alias of this thread's connection. It is
            opened if needed.
    """
    if isinstance(conn, str):
        conn = connections[conn]
    conn.ensure_connection()
    with conn.connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('rls.current_tenant', true), "
            "current_setting('rls.is_admin', true)"
        )
        return tuple(value or None for value in cursor.fetchone())
