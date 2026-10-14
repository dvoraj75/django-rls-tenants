"""GUC affinity guard: keep each connection's RLS GUCs in step with the active RLS context.

The library sets the admin and tenant GUCs when a context starts, on the
connection that is current at that moment. SQL can still run on another
connection while the context is live, or on the same connection after
something else has changed the GUCs:

- a ``sync_to_async(..., thread_sensitive=False)`` hop runs on another thread,
  with a copy of the context's ``ContextVar`` values but with that thread's
  own connections; so does the error page Django builds there under ASGI;
- sibling ``asyncio`` tasks of one request share its connection, so one
  task's context can change the GUCs between another task's statements;
- a connection keeps the GUCs a hop put on it, and a context entered on one
  alias leaves the other aliases as they were.

:func:`install` puts :func:`_guard` first in the ``execute_wrappers`` of each
connection of an alias in ``RLS_TENANTS["DATABASES"]``, so Django runs every
statement on it through the guard. Before each statement the guard compares
the GUC values of the active context (``state._get_context_gucs()``, set only
by the library's own entry points) with the values the library last left on
that database session. When they differ, it applies the context's values in
one ``set_config()`` statement first: a hop inherits the context, a task gets
its own values back, and a connection used outside any context loses the
values a context left on it. If that statement fails, the error propagates
and the original statement does not run.

The library's own GUC writes run inside :func:`writing`, which keeps the guard
out of the way and records the values they leave. Writes of the same GUCs by
other code (``set_guc()``, ``rls_context()``) are not recorded, so they stay
in force until the context changes, as before.

PostgreSQL undoes a ``set_config()`` made in a transaction or savepoint that
is rolled back. After a transaction ends, a value recorded during it is kept
only if it equals the value from before the transaction, since commit and
rollback then leave the same thing. After a ``ROLLBACK TO SAVEPOINT``, once
any value changed in the transaction, nothing recorded is trusted. In both
cases the next statement applies the context's values again. The guard reads
the transaction status from the driver (libpq), without a round trip.

Limits: the guard only works with session GUCs; with ``USE_LOCAL_SET=True`` it
does nothing and never writes a session GUC. It covers the admin and tenant
GUCs, not bypass flags or other GUCs set with ``rls_context()``. It cannot
see SQL that bypasses Django's cursor wrapper: a raw driver cursor, or the
``FETCH`` of a server-side cursor that is already open.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

from django.db import connections
from django.utils.connection import ConnectionDoesNotExist

from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.state import _get_context_gucs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper

logger = logging.getLogger("django_rls_tenants")

# libpq's transaction status (PQtransactionStatus), which both drivers report as is.
_IDLE: Final = 0
_IN_TRANSACTION: Final = 2

# One statement for both GUCs: names and values are bind parameters.
_SET_GUCS_SQL: Final = "SELECT set_config(%s, %s, %s), set_config(%s, %s, %s)"

# Attribute holding a guarded connection's _SessionGUCs.
_TRACKER_ATTR: Final = "_rls_session_gucs"

# Depth of the library's own GUC writes on this thread (see writing()). One
# thread runs one connection's statements at a time, sync_to_async hops included.
_writes = threading.local()


class _Unknown:
    """Marker for GUC values that are not known; the next statement applies the context's."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN: Final = _Unknown()

# (is_admin, current_tenant) as the library left them; None when no context's
# values are in force (the session's own, or cleared); or UNKNOWN.
type _Tracked = tuple[str, str] | _Unknown | None


class _SessionGUCs:
    """What the library knows about the admin and tenant GUCs of one database session.

    Attributes:
        raw: The DB-API connection (the session) the other attributes describe.
        values: The admin and tenant GUC values the library left on it.
        in_transaction: A transaction was open when the session was last looked at.
        base: ``values`` when that transaction started.
        changed: ``values`` changed during that transaction.
        savepoints: The transaction's savepoints, oldest first: each name with
            ``values`` when it was set.
    """

    __slots__ = (
        "base",
        "changed",
        "in_transaction",
        "raw",
        "read_status",
        "savepoints",
        "values",
    )

    def __init__(self) -> None:
        self.raw: object = None
        self.read_status: Callable[[], int] = _no_status
        self.values: _Tracked = UNKNOWN
        self.in_transaction = False
        self.base: _Tracked = UNKNOWN
        self.changed = False
        self.savepoints: list[tuple[str, _Tracked]] = []

    def start(self, raw: Any, values: _Tracked) -> None:
        """Start tracking a new session, ``raw``, whose GUCs hold ``values``."""
        self.raw = raw
        self.read_status = _status_reader(raw)
        self.values = values
        self.in_transaction = False
        self.base = values
        self.changed = False
        self.savepoints.clear()


def _no_status() -> int:
    """Stand-in status reader for a tracker that has no session yet."""
    return _IDLE


def _status_reader(raw: Any) -> Callable[[], int]:
    """Return a function reading libpq's transaction status of ``raw``, without a query.

    Args:
        raw: A psycopg 3 or psycopg2 connection.
    """
    pgconn = getattr(raw, "pgconn", None)
    if pgconn is not None:  # psycopg 3
        return lambda: int(pgconn.transaction_status)
    get_status: Callable[[], int] = raw.get_transaction_status  # psycopg2
    return get_status


def install(connection: BaseDatabaseWrapper, *, values: _Tracked) -> None:
    """Guard ``connection`` and start tracking the database session it has just opened.

    The ``connection_created`` handler calls it for each alias in
    ``RLS_TENANTS["DATABASES"]``, on every connect and every checkout from a
    connection pool.

    Args:
        connection: The connection, as sent by ``connection_created``.
        values: What the new session's admin and tenant GUCs hold: ``None`` for
            a new session, ``UNKNOWN`` for one a pool has lent out before.
    """
    if connection.vendor != "postgresql":
        return
    if _guard not in connection.execute_wrappers:
        # First, not last: connection.execute_wrapper() removes the last
        # wrapper when its block exits, which would be the guard if the
        # connection opened inside such a block.
        connection.execute_wrappers.insert(0, _guard)
    tracker: _SessionGUCs | None = getattr(connection, _TRACKER_ATTR, None)
    if tracker is None:
        tracker = _SessionGUCs()
        setattr(connection, _TRACKER_ATTR, tracker)
    tracker.start(connection.connection, values)


def _is_guarded(connection: BaseDatabaseWrapper) -> bool:
    """Whether the guard applies the context's GUCs on ``connection`` (session GUC mode only)."""
    conf = rls_tenants_config
    return not conf.USE_LOCAL_SET and connection.alias in conf.DATABASES


def _observe(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> int:
    """Bring ``tracker`` up to date with the session's transaction status, and return it.

    A transaction that has ended since the last look either committed the
    values recorded during it or rolled back to ``base``. The recorded values
    are kept only when the two agree.
    """
    raw = connection.connection
    if raw is not tracker.raw:
        # A session install() did not see open, such as one an alias had
        # before it joined RLS_TENANTS["DATABASES"].
        tracker.start(raw, UNKNOWN)
    status = tracker.read_status()
    if status == _IDLE:
        if tracker.in_transaction:
            tracker.in_transaction = False
            tracker.savepoints.clear()
            if tracker.values != tracker.base:
                tracker.values = UNKNOWN
    elif not tracker.in_transaction:
        tracker.in_transaction = True
        tracker.base = tracker.values
        tracker.changed = False
        tracker.savepoints.clear()
    return status


def _connection_of(target: BaseDatabaseWrapper | str) -> BaseDatabaseWrapper | None:
    """Return the connection ``target`` names, or ``None`` for an alias without one.

    Args:
        target: A connection, or an alias for the thread's connection of that alias.
    """
    if not isinstance(target, str):
        return target
    try:
        return connections[target]
    except ConnectionDoesNotExist:
        return None


def record(target: BaseDatabaseWrapper | str, values: _Tracked) -> None:
    """Note that the library has left the admin and tenant GUCs of a connection at ``values``.

    A no-op on a connection that is closed: its next session is tracked from
    the start. A connection that opened before the ``connection_created``
    handler was connected (during another app's ``ready()``, say) gets the
    guard installed here, with ``UNKNOWN`` values. An alias the guard leaves
    alone at the moment (``USE_LOCAL_SET=True``, or not in
    ``RLS_TENANTS["DATABASES"]``) gets ``UNKNOWN`` too, since the guard did
    not reconcile the session before the write.

    Args:
        target: The connection written to, or its alias.
        values: ``(is_admin, current_tenant)``, ``None`` for cleared GUCs, or
            ``UNKNOWN``.
    """
    connection = _connection_of(target)
    if connection is None or connection.connection is None:
        return
    tracker: _SessionGUCs | None = getattr(connection, _TRACKER_ATTR, None)
    if tracker is None:
        install(connection, values=UNKNOWN)
        return
    if not _is_guarded(connection):
        values = UNKNOWN
    _observe(connection, tracker)
    if values != tracker.values:
        tracker.values = values
        if tracker.in_transaction:
            tracker.changed = True


def _forget(target: BaseDatabaseWrapper | str) -> None:
    """Mark a connection's GUC values unknown, without touching the driver."""
    connection = _connection_of(target)
    tracker: _SessionGUCs | None = getattr(connection, _TRACKER_ATTR, None)
    if tracker is not None:
        tracker.values = UNKNOWN
        tracker.changed = True


@contextmanager
def writing(target: BaseDatabaseWrapper | str, values: _Tracked) -> Iterator[None]:
    """Run the library's own writes of the admin and tenant GUCs on ``connection``.

    The guard does not reconcile statements inside the block. When the block
    exits, ``values`` is recorded as what it left on the session, or
    ``UNKNOWN`` if it raised. A block that restores a context's snapshot passes
    the values of the context that is active again: the guard reconciled the
    session to that context before the snapshot was read, so the restore puts
    those values back.

    Args:
        target: The connection the block writes to, or its alias (the thread's
            connection of that alias, looked up when the block exits).
        values: ``(is_admin, current_tenant)`` as the block leaves them, or
            ``None`` for cleared GUCs.
    """
    _writes.depth = getattr(_writes, "depth", 0) + 1
    try:
        yield
    except BaseException:
        _forget(target)
        raise
    else:
        record(target, values)
    finally:
        _writes.depth -= 1


def set_gucs(
    connection: BaseDatabaseWrapper,
    values: tuple[str, str] | None,
    *,
    is_local: bool = False,
) -> None:
    """Set the admin and tenant GUCs of ``connection`` to ``values`` in one statement.

    Runs on the given connection, not on the thread's connection for its
    alias, and records the values.

    Args:
        connection: The connection to write to.
        values: ``(is_admin, current_tenant)``, or ``None`` to clear both.
        is_local: Set them transaction-locally (``SET LOCAL`` semantics).

    Raises:
        RuntimeError: If ``is_local=True`` outside ``transaction.atomic()``.
    """
    if is_local and not connection.in_atomic_block:
        msg = (
            f"Cannot set the RLS GUCs on alias {connection.alias!r} transaction-locally "
            "outside a transaction. Wrap the code in transaction.atomic() or set "
            "RLS_TENANTS['USE_LOCAL_SET'] = False."
        )
        raise RuntimeError(msg)
    conf = rls_tenants_config
    is_admin, tenant = ("", "") if values is None else values
    with writing(connection, values), connection.cursor() as cursor:
        cursor.execute(
            _SET_GUCS_SQL,
            [conf.GUC_IS_ADMIN, is_admin, is_local, conf.GUC_CURRENT_TENANT, tenant, is_local],
        )


def _reconcile(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> None:
    """Apply the active context's GUC values on ``connection`` if its session holds others.

    Skipped where a statement cannot run anyway (a failed transaction, or one
    Django has marked for rollback), so that statement's own error surfaces.
    """
    status = _observe(connection, tracker)
    if status not in {_IDLE, _IN_TRANSACTION} or connection.needs_rollback:
        return
    if not _is_guarded(connection):
        return
    context_gucs = _get_context_gucs()
    held = tracker.values
    if context_gucs == held:
        return
    set_gucs(connection, context_gucs)
    logger.debug(
        "GUC affinity guard: alias %r held %r; applied the active context's %r",
        connection.alias,
        held,
        context_gucs,
    )


def reconcile(connection: BaseDatabaseWrapper) -> None:
    """Apply the active context's GUC values on ``connection`` now, as the guard would.

    For code that reads a snapshot of the GUCs inside :func:`writing`, where
    the guard stays out of the way. Opens the connection if needed.
    """
    connection.ensure_connection()
    tracker: _SessionGUCs | None = getattr(connection, _TRACKER_ATTR, None)
    if tracker is not None:
        _reconcile(connection, tracker)


# Statements that only control the transaction: they read no data, so the guard
# does not apply the context before them. Failing there would break Django's
# own transaction handling (a savepoint rollback, say).
_TRANSACTION_CONTROL: Final = frozenset(
    {"ABORT", "BEGIN", "COMMIT", "END", "RELEASE", "ROLLBACK", "SAVEPOINT", "START"},
)

# Statements after which the tracker is updated: transaction control, and
# session resets (RESET ALL, DISCARD ALL, RESET of the admin or tenant GUC).
_FOLLOWED: Final = _TRANSACTION_CONTROL | {"DISCARD", "RESET"}


def _command_of(sql: object) -> str:
    """Return the first word of ``sql``, upper-cased, or ``""`` if there is none."""
    if not isinstance(sql, str):
        return ""
    words = sql[:128].split(None, 1)
    return words[0].upper() if words else ""


def _identifier(word: str) -> str:
    """Return a SQL identifier as PostgreSQL compares it: quoted keeps its case."""
    word = word.rstrip(";")
    if len(word) > 1 and word[0] == word[-1] == '"':
        return word[1:-1].replace('""', '"')
    return word.lower()


def _savepoint_index(tracker: _SessionGUCs, name: str) -> int | None:
    """Return the position of the newest savepoint called ``name``, or ``None``."""
    for index in range(len(tracker.savepoints) - 1, -1, -1):
        if tracker.savepoints[index][0] == name:
            return index
    return None


def _follow(
    command: str,
    sql: str,
    connection: BaseDatabaseWrapper,
    tracker: _SessionGUCs,
) -> None:
    """Update ``tracker`` after a statement in ``_FOLLOWED`` has run.

    A savepoint records the values at that point, and ``ROLLBACK TO`` it puts
    them back, as PostgreSQL does with the GUCs. A ``ROLLBACK`` of the whole
    transaction brings back the values from before it, and a ``COMMIT`` keeps
    the current ones. ``RESET ALL``, ``DISCARD ALL`` and ``RESET`` of the admin
    or tenant GUC reset them. Anything the tracker cannot follow (a savepoint
    it did not see set, say) makes the values unknown once they changed during
    the transaction.

    Args:
        command: The statement's first word, upper-cased (``_command_of()``).
        sql: The statement.
        connection: The connection it ran on.
        tracker: The connection's tracker.
    """
    words = sql[:256].split()
    argument = words[1].rstrip(";").upper() if len(words) > 1 else ""
    if command in {"RESET", "DISCARD"}:
        conf = rls_tenants_config
        names = {conf.GUC_IS_ADMIN.upper(), conf.GUC_CURRENT_TENANT.upper()}
        if argument == "ALL" or (command == "RESET" and argument in names):
            tracker.values = UNKNOWN
        return
    if argument == "PREPARED":  # two-phase commit: another transaction
        return
    rolls_back_to = command == "ROLLBACK" and "TO" in (word.upper() for word in words)
    if command in {"ROLLBACK", "ABORT"} and not rolls_back_to:
        if tracker.in_transaction:
            tracker.values = tracker.base
        tracker.savepoints.clear()
    elif command in {"COMMIT", "END"}:
        tracker.base = tracker.values
        tracker.savepoints.clear()
    # Picks up a transaction this statement opened or closed.
    _observe(connection, tracker)
    if command == "SAVEPOINT" and len(words) > 1:
        tracker.savepoints.append((_identifier(words[1]), tracker.values))
    elif command == "RELEASE" or rolls_back_to:
        index = _savepoint_index(tracker, _identifier(words[-1]))
        if index is None:
            if tracker.changed:
                tracker.values = UNKNOWN
        elif rolls_back_to:
            # The savepoint stays after a rollback to it.
            tracker.values = tracker.savepoints[index][1]
            del tracker.savepoints[index + 1 :]
        else:
            del tracker.savepoints[index:]


def _guard(
    execute: Callable[[str, Any, bool, dict[str, Any]], Any],
    sql: str,
    params: Any,
    many: bool,
    context: dict[str, Any],
) -> Any:
    """Execute wrapper: apply the active context's GUCs before the statement if needed."""
    connection: BaseDatabaseWrapper = context["connection"]
    tracker: _SessionGUCs | None = getattr(connection, _TRACKER_ATTR, None)
    if tracker is None:
        return execute(sql, params, many, context)
    command = _command_of(sql)
    if getattr(_writes, "depth", 0) or command in _TRANSACTION_CONTROL:
        # The library's own write, or no data read: only follow the transaction.
        _observe(connection, tracker)
    else:
        _reconcile(connection, tracker)
    try:
        result = execute(sql, params, many, context)
    except BaseException:
        if command in _TRANSACTION_CONTROL:
            tracker.values = UNKNOWN
        raise
    if command in _FOLLOWED:
        _follow(command, sql, connection, tracker)
    return result
