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
- a context entered on one alias leaves the other aliases as they were.

:func:`install` puts :func:`_guard` first in the ``execute_wrappers`` of every
PostgreSQL connection, so Django runs every statement on it through the guard.
On a connection of an alias in ``RLS_TENANTS["DATABASES"]``, before each
statement, the guard compares the GUC values of the active context
(``state._get_context_gucs()``, set only by the library's own entry points and
ended for every copy when the context ends) with the values the library last
left on that database session. When they differ, it applies the context's
values in one ``set_config()`` statement first: a hop inherits the context, a
task gets its own values back, and a connection used outside any context loses
the values a context left on it. If that statement fails, the error propagates
and the original statement does not run.

With ``USE_LOCAL_SET=True`` (#96) the guard writes the values
transaction-locally (``set_config(..., true)``), so they end with the
transaction they were written in, committed or not, and the session never
keeps them. In a transaction it applies them as above. A statement run in
autocommit while a context is active gets a transaction of its own:
``BEGIN``, the context's values, the statement, ``COMMIT``. The guard sends
``BEGIN``, ``COMMIT`` and ``ROLLBACK`` past Django's cursor, so no execute
wrapper sees them, and logs them as Django logs its own transaction control
(in ``connection.queries`` and on the ``django.db.backends`` logger). A
statement PostgreSQL may refuse inside a transaction block (``VACUUM``,
``CREATE INDEX CONCURRENTLY``, ...) runs as it is, without the context; none
of them evaluates an RLS policy. ``SET TRANSACTION`` is transaction control:
the guard writes nothing before it, in either mode. When a context ends
(:func:`suspect_sessions`), and on any connection after a statement that ran
under a context's transaction-local values, what the session holds for itself
counts as unknown, since code may have written the GUCs for the session there
(``set_guc()``, ``rls_context()`` without ``is_local=True``). Outside any
context the guard clears the session's own values when they are unknown, or
a context's (after a mode change, say), for the session, in a transaction too.

The library's own GUC writes run inside :func:`writing`, which keeps the guard
out of the way and records the values they leave. Writes of the same GUCs by
other code (``set_guc()``, ``rls_context()``) are not recorded, so they stay
in force until the context changes, as before, with one exception: with
``USE_LOCAL_SET=True`` a statement run in autocommit inside a context gets
the context's values in its own transaction, which override them.

PostgreSQL undoes a ``set_config()`` made in a transaction or savepoint that
is rolled back, and a transaction-local one at the end of its transaction,
committed or not. The tracker follows the transaction status the driver reports
(libpq, without a round trip), Django's commit and rollback of the connection
(``install()`` wraps them), and the transaction-control statements that run
through Django's cursor: a savepoint keeps the values it was set with, and a
``ROLLBACK TO`` it puts them back. Whatever it cannot follow (a transaction a
driver cursor ended, a savepoint it did not see set, a statement it cannot
read) makes the values unknown, and the next statement applies the context's
values again.

Limits: the guard covers the admin and tenant GUCs, not bypass flags or other
GUCs set with ``rls_context()``. With ``USE_LOCAL_SET=True``, a procedure or
``DO`` block that commits or rolls back cannot run inside a context in
autocommit (PostgreSQL refuses it inside the guard's transaction), and an
``executemany()`` there commits or fails as a whole; a statement PostgreSQL
allows only in a transaction block (``DECLARE`` of a cursor without ``WITH
HOLD``, ``LOCK TABLE``, ``SET LOCAL``) runs there inside the guard's
transaction, and its effect ends at the guard's ``COMMIT``. It cannot see SQL
that bypasses Django's execute wrappers (a driver cursor,
``CursorWrapper.callproc()``, the driver's ``copy`` methods, the ``FETCH`` of
a server-side cursor that is already open); on a connection the guard has
used, such SQL sees whatever values it left there until the next statement it
sees (with ``USE_LOCAL_SET=True``, outside a transaction, none). It assumes
that a new session holds no value for the two GUCs: a default set with
``ALTER DATABASE ... SET`` or ``ALTER ROLE ... SET`` is in force in every new
session and is never cleared (system check ``W011`` warns about one).
"""

from __future__ import annotations

import logging
import string
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple

from django.db import DatabaseError, connections
from django.db.backends.utils import debug_transaction
from django.utils.connection import ConnectionDoesNotExist

from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.state import ContextGucs, _get_context_gucs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper

logger = logging.getLogger("django_rls_tenants")

# libpq's transaction status (PQtransactionStatus), which both drivers report as is.
_IDLE: Final = 0
_IN_ERROR: Final = 3
_CONNECTION_BAD: Final = 4

# One statement for both GUCs: names and values are bind parameters.
_SET_GUCS_SQL: Final = "SELECT set_config(%s, %s, %s), set_config(%s, %s, %s)"

# Attributes of a guarded connection: its tracker, and the marker that its
# commit and rollback are followed.
_TRACKER_ATTR: Final = "_rls_session_gucs"
_HOOKED_ATTR: Final = "_rls_transaction_ends_followed"

# How much of a statement is read to find its command and arguments.
_SQL_HEAD: Final = 256

# Statements that only control the transaction: they read no data, so the guard
# does not apply the context before them. Failing there would break Django's
# own transaction handling (a savepoint rollback, say).
_TRANSACTION_CONTROL: Final = frozenset(
    {"ABORT", "BEGIN", "COMMIT", "END", "PREPARE", "RELEASE", "ROLLBACK", "SAVEPOINT", "START"},
)

# Statements after which the tracker is updated: transaction control, and
# session resets (RESET ALL, DISCARD ALL, RESET of the admin or tenant GUC).
_FOLLOWED: Final = _TRANSACTION_CONTROL | {"DISCARD", "RESET"}

# Commands PostgreSQL refuses inside a transaction block, in all or some of
# their forms (see _refused_in_a_transaction()). None of them evaluates an RLS
# policy, so with USE_LOCAL_SET=True the guard runs them as they are.
_NO_TRANSACTION_COMMANDS: Final = frozenset({"CLUSTER", "DISCARD", "REINDEX", "VACUUM"})
_NO_TRANSACTION_OBJECTS: Final = frozenset({"DATABASE", "SUBSCRIPTION", "SYSTEM", "TABLESPACE"})
_DDL_COMMANDS: Final = frozenset({"ALTER", "CREATE", "DROP"})

# PostgreSQL folds unquoted identifiers to lower case for ASCII letters only.
_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


class _WriteDepth(threading.local):
    """Depth of the library's own GUC writes on this thread (see :func:`writing`).

    One thread runs one connection's statements at a time, ``sync_to_async``
    hops included.
    """

    depth = 0


_writes = _WriteDepth()


class _Unknown:
    """Marker for GUC values that are not known; the next statement applies the context's."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN: Final = _Unknown()

# The values the library left on a session; None when no context's values are in
# force (the session's own, or cleared); or UNKNOWN.
type _Tracked = ContextGucs | _Unknown | None


# ---- The tracker ----


def _no_status() -> int:
    """Stand-in status reader for a tracker that has no session yet."""
    return _IDLE


def _status_reader(raw: Any) -> Callable[[], int]:  # Any: a psycopg or psycopg2 connection
    """Return a function reading libpq's transaction status of ``raw``, without a query.

    Args:
        raw: A psycopg 3 or psycopg2 connection.
    """
    pgconn = getattr(raw, "pgconn", None)
    if pgconn is not None:  # psycopg 3
        return lambda: int(pgconn.transaction_status)
    get_status: Callable[[], int] = raw.get_transaction_status  # psycopg2
    return get_status


class _SessionGUCs:
    """What the library knows about the admin and tenant GUCs of one database session.

    Attributes:
        raw: The DB-API connection (the session) the other attributes describe.
        read_status: Reads the session's transaction status from the driver.
        values: The admin and tenant GUC values the library left on it, as
            its statements see them.
        in_transaction: A transaction was open when the session was last looked at.
        base: ``values`` when that transaction started: the session's own,
            since no transaction-local value outlives its transaction.
        changed: ``values`` changed during that transaction.
        wrote_local: Values were written transaction-locally during it.
        wrote_session: Values were written for the session during it, or
            changed in a way that was not followed.
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
        "wrote_local",
        "wrote_session",
    )

    def __init__(self) -> None:
        self.raw: object = None
        self.read_status: Callable[[], int] = _no_status
        self.values: _Tracked = UNKNOWN
        self.in_transaction = False
        self.base: _Tracked = UNKNOWN
        self.changed = False
        self.wrote_local = False
        self.wrote_session = False
        self.savepoints: list[tuple[str, _Tracked]] = []

    def attach(self, raw: Any, values: _Tracked) -> None:  # Any: a DB-API connection
        """Start tracking a new session, ``raw``, whose GUCs hold ``values``."""
        self.raw = raw
        self.read_status = _status_reader(raw)
        self.values = values
        self.in_transaction = False
        self.base = values
        self.changed = self.wrote_local = self.wrote_session = False
        self.savepoints.clear()

    def sync_transaction(self) -> int:
        """Bring the record up to date with the session's transaction status, and return it.

        A transaction that has ended unseen since the last look either
        committed or rolled back (see :meth:`end_transaction`).
        """
        status = self.read_status()
        if status == _IDLE:
            if self.in_transaction:
                self.end_transaction(committed=None)
        elif not self.in_transaction:
            self.in_transaction = True
            self.base = self.values
            self.changed = self.wrote_local = self.wrote_session = False
            self.savepoints.clear()
        return status

    def set_values(self, values: _Tracked, *, local: bool = False) -> None:
        """Record ``values`` as what the session holds.

        Args:
            values: The values written.
            local: They were written transaction-locally (``SET LOCAL``).
        """
        if self.in_transaction:
            if local:
                self.wrote_local = True
            else:
                self.wrote_session = True
        if values != self.values:
            self.values = values
            if self.in_transaction:
                self.changed = True

    def forget(self) -> None:
        """Record that the values are not known, as after a change for the session."""
        self.set_values(UNKNOWN)

    def suspect_session(self) -> None:
        """Record that the session's own values may have changed unseen (see ``suspect_sessions``).

        Outside a transaction they are unknown now. In one, a commit leaves
        them unknown and a rollback undoes the change, as for a session-level
        write under the transaction's local ones.
        """
        if self.in_transaction:
            self.wrote_session = self.wrote_local = True
        else:
            self.values = UNKNOWN

    def end_transaction(self, *, committed: bool | None) -> None:
        """Follow the end of the transaction, and what it leaves on the session.

        A rollback undoes every value written during the transaction, and any
        end undoes the transaction-local ones; a commit keeps those written
        for the session. When both kinds were written, a commit keeps the last
        value written for the session, which the tracker does not hold
        separately from the local one, so the values become unknown.

        Args:
            committed: Whether the transaction committed; ``None`` if that is
                not known, which keeps values written for the session only
                when both outcomes agree.
        """
        if self.in_transaction:
            if committed is False or not self.wrote_session:
                self.values = self.base
            elif self.wrote_local or (committed is None and self.values != self.base):
                self.values = UNKNOWN
            self.in_transaction = False
        self.savepoints.clear()

    def savepoint_index(self, name: str) -> int | None:
        """Return the position of the newest savepoint called ``name``, or ``None``."""
        for index in range(len(self.savepoints) - 1, -1, -1):
            if self.savepoints[index][0] == name:
                return index
        return None


def _tracker_of(connection: BaseDatabaseWrapper | None) -> _SessionGUCs | None:
    """Return ``connection``'s tracker, or ``None`` if it has none (or is ``None``)."""
    tracker: _SessionGUCs | None = getattr(connection, _TRACKER_ATTR, None)
    return tracker


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


def _is_guarded(connection: BaseDatabaseWrapper) -> bool:
    """Whether the guard applies the context's GUCs on ``connection`` (its alias is configured)."""
    return connection.alias in rls_tenants_config.DATABASES


def _will_run_in_transaction(connection: BaseDatabaseWrapper, status: int) -> bool:
    """Whether a statement run on ``connection`` now runs in a transaction block.

    One is open (``status``, the session's transaction status, is not idle),
    or the driver opens one for the statement because Django has turned
    autocommit off (``atomic()``, ``AUTOCOMMIT=False``).
    """
    return status != _IDLE or not connection.autocommit


def _observe(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> int:
    """Bring ``tracker`` up to date with ``connection``'s session, and return its status.

    A closed connection (a cursor used after ``connection.close()``) has no
    session to reconcile: the statement fails on its own, with Django's error.
    """
    raw = connection.connection
    if raw is None:
        return _CONNECTION_BAD
    if raw is not tracker.raw:
        # A session install() did not see open, such as one an alias had
        # before it joined RLS_TENANTS["DATABASES"].
        tracker.attach(raw, UNKNOWN)
    return tracker.sync_transaction()


# ---- Installing the guard ----


def install(connection: BaseDatabaseWrapper, *, values: _Tracked) -> None:
    """Guard ``connection`` and track the database session it has just opened.

    The ``connection_created`` handler calls it for every PostgreSQL
    connection, on every connect and every checkout from a connection pool;
    the guard itself ignores aliases outside ``RLS_TENANTS["DATABASES"]``.

    Args:
        connection: The connection, as sent by ``connection_created``.
        values: What the new session's admin and tenant GUCs hold: ``None`` for
            a new session, ``UNKNOWN`` for one a pool has lent out before. A new
            session the guard already tracks (a statement of another
            ``connection_created`` receiver ran through it) keeps its record.
    """
    if connection.vendor != "postgresql":
        return
    if _guard not in connection.execute_wrappers:
        # First, not last: connection.execute_wrapper() removes the last
        # wrapper when its block exits, which would be the guard if the
        # connection opened inside such a block.
        connection.execute_wrappers.insert(0, _guard)
    _follow_transaction_ends(connection)
    tracker = _tracker_of(connection)
    if tracker is None:
        tracker = _SessionGUCs()
        setattr(connection, _TRACKER_ATTR, tracker)
    if values is UNKNOWN or tracker.raw is not connection.connection:
        tracker.attach(connection.connection, values)


def guard_open_connection(connection: BaseDatabaseWrapper) -> None:
    """Guard ``connection`` if it is open and the guard has not seen it open.

    For a connection opened before the ``connection_created`` handler was
    connected (in another app's ``ready()``, say). Its session's values are
    unknown, so the guard applies the context before its first statement.
    Session GUC writes install the guard on such a connection too (see
    :func:`_record`), but with ``USE_LOCAL_SET=True`` a context writes
    nothing: only the guard applies it.
    """
    if connection.connection is not None and _tracker_of(connection) is None:
        install(connection, values=UNKNOWN)


def _follow_transaction_ends(connection: BaseDatabaseWrapper) -> None:
    """Make ``connection``'s commit and rollback update its tracker.

    Django ends the outermost transaction (``atomic()``, ``transaction.commit()``
    / ``rollback()``) through ``_commit()`` / ``_rollback()``, on the driver
    connection, not through a cursor. Following them tells the tracker which of
    the two happened, instead of leaving it to find out from the transaction
    status that the transaction ended, possibly too late.
    """
    if getattr(connection, _HOOKED_ATTR, False):
        return
    for name, committed in (("_commit", True), ("_rollback", False)):
        end: Callable[[], Any] = getattr(connection, name)
        # An instance attribute shadows Django's method for this connection only.
        setattr(connection, name, _transaction_end(connection, end, committed=committed))
    setattr(connection, _HOOKED_ATTR, True)


def _transaction_end(
    connection: BaseDatabaseWrapper,
    end: Callable[[], Any],
    *,
    committed: bool,
) -> Callable[[], Any]:
    """Wrap Django's ``_commit`` or ``_rollback`` of ``connection`` to update its tracker.

    The transaction status before and after tells what happened: only a
    transaction that was open and is now closed has ended (psycopg2 sends
    nothing in autocommit mode), and PostgreSQL turns the commit of a failed
    transaction into a rollback, without an error.
    """

    def end_and_follow() -> Any:  # Any: what Django's _commit / _rollback return
        raw = connection.connection
        tracker = _tracker_of(connection)
        if tracker is None or raw is None or raw is not tracker.raw:
            return end()
        before = tracker.read_status()
        try:
            result = end()
        except DatabaseError:
            # A commit that failed (a deferred constraint, say) rolled back.
            _follow_end(tracker, before, committed=False)
            raise
        except BaseException:
            # Raised around a commit that may have gone through (an interruption).
            _follow_end(tracker, before, committed=None if committed else False)
            raise
        _follow_end(tracker, before, committed=committed)
        return result

    return end_and_follow


def _follow_end(tracker: _SessionGUCs, before: int, *, committed: bool | None) -> None:
    """Follow a commit or rollback the driver was asked for, from the status before and after.

    Args:
        tracker: The connection's tracker.
        before: The transaction status before the call.
        committed: Whether it was a commit; ``None`` for a commit that may
            or may not have gone through.
    """
    after = tracker.read_status()
    if before != _IDLE and after == _IDLE:
        tracker.end_transaction(committed=False if before == _IN_ERROR else committed)
    elif before != after:
        tracker.forget()


# ---- The library's own writes ----


def _record(target: BaseDatabaseWrapper | str, values: _Tracked, *, local: bool = False) -> None:
    """Note that the library has left the admin and tenant GUCs of a connection at ``values``.

    A no-op on a connection that is closed: its next session is tracked from
    the start. A connection that opened before the ``connection_created``
    handler was connected (during another app's ``ready()``, say) gets the
    guard installed here, with ``UNKNOWN`` values. An alias the guard leaves
    alone (not in ``RLS_TENANTS["DATABASES"]``) gets ``UNKNOWN`` too, since
    the guard did not reconcile the session before the write.

    Args:
        target: The connection written to, or its alias.
        values: The values, ``None`` for cleared GUCs, or ``UNKNOWN``.
        local: They were written transaction-locally (``SET LOCAL``).
    """
    connection = _connection_of(target)
    if connection is None or connection.connection is None:
        return
    tracker = _tracker_of(connection)
    if tracker is None:
        install(connection, values=UNKNOWN)
        return
    _observe(connection, tracker)
    tracker.set_values(values if _is_guarded(connection) else UNKNOWN, local=local)


def _forget(target: BaseDatabaseWrapper | str) -> None:
    """Mark a connection's GUC values unknown, without touching the driver."""
    tracker = _tracker_of(_connection_of(target))
    if tracker is not None:
        tracker.forget()


@contextmanager
def writing(
    target: BaseDatabaseWrapper | str,
    values: _Tracked,
    *,
    local: bool = False,
) -> Iterator[None]:
    """Mark a block as the library's own write of the admin and tenant GUCs on ``target``.

    The guard does not reconcile statements inside the block. When the block
    exits, ``values`` is recorded as what it left on the session, or
    ``UNKNOWN`` if it raised. A block that restores a context's snapshot passes
    the values of the context that is active again: the guard reconciled the
    session to that context before the snapshot was read, so the restore puts
    those values back.

    Args:
        target: The connection the block writes to, or its alias (the thread's
            connection of that alias, looked up when the block exits).
        values: The values the block leaves, or ``None`` for cleared GUCs.
        local: The block writes them transaction-locally (``SET LOCAL``), so
            they end with the transaction.
    """
    depth = _writes.depth
    _writes.depth = depth + 1
    try:
        yield
    except BaseException:
        _forget(target)
        raise
    else:
        _record(target, values, local=local)
    finally:
        _writes.depth = depth


def write_gucs(
    connection: BaseDatabaseWrapper,
    values: ContextGucs | None,
    *,
    is_local: bool = False,
) -> None:
    """Set the admin and tenant GUCs of ``connection`` to ``values`` in one statement.

    Runs on the given connection, not on the thread's connection for its
    alias, and records the values.

    Args:
        connection: The connection to write to.
        values: The context's values, or ``None`` to clear both GUCs.
        is_local: Set them transaction-locally (``SET LOCAL`` semantics), as
            the guard does with ``USE_LOCAL_SET=True``.

    Raises:
        RuntimeError: If ``is_local=True`` and the statement would not run in
            a transaction block.
    """
    if is_local:
        connection.ensure_connection()
        if not _will_run_in_transaction(connection, _status_reader(connection.connection)()):
            msg = (
                f"Cannot set the RLS GUCs on alias {connection.alias!r} transaction-locally "
                "outside a transaction. Wrap the code in transaction.atomic() or set "
                "RLS_TENANTS['USE_LOCAL_SET'] = False."
            )
            raise RuntimeError(msg)
    conf = rls_tenants_config
    is_admin, tenant = ("", "") if values is None else values
    with writing(connection, values, local=is_local), connection.cursor() as cursor:
        cursor.execute(
            _SET_GUCS_SQL,
            [conf.GUC_IS_ADMIN, is_admin, is_local, conf.GUC_CURRENT_TENANT, tenant, is_local],
        )


# ---- Applying the active context ----


def _apply_if_stale(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> int:
    """Apply the active context's GUC values on ``connection`` if its session holds others.

    Skipped where a statement cannot run anyway (a failed transaction or
    connection, or a transaction Django has marked for rollback), so that
    statement's own error surfaces.

    With ``USE_LOCAL_SET=True`` a context's values are written
    transaction-locally, in a transaction; in autocommit they are not written
    here: :func:`_guard` runs the statement in a transaction of its own, with
    them. Outside any context the session's own values are cleared, in a
    transaction or not.

    Returns:
        The session's transaction status before any statement ran.
    """
    status = _observe(connection, tracker)
    context_gucs = _get_context_gucs()
    held = tracker.values
    if context_gucs == held:
        return status
    if status in {_IN_ERROR, _CONNECTION_BAD} or connection.needs_rollback:
        return status
    if not _is_guarded(connection):
        return status
    # With USE_LOCAL_SET=True a context's values are only ever set
    # transaction-locally; without a context the session's own are cleared,
    # in a transaction too, so the clear outlives it.
    is_local = context_gucs is not None and rls_tenants_config.USE_LOCAL_SET
    if is_local and not _will_run_in_transaction(connection, status):
        # In autocommit: _guard runs the statement in a transaction of its own.
        return status
    write_gucs(connection, context_gucs, is_local=is_local)
    logger.debug(
        "GUC affinity guard: alias %r held %r; applied the active context's %r",
        connection.alias,
        held,
        context_gucs,
    )
    return status


def apply_active_context(connection: BaseDatabaseWrapper) -> None:
    """Apply the active context's GUC values on ``connection`` now, as the guard would.

    For code that reads a snapshot of the GUCs inside :func:`writing`, where
    the guard stays out of the way, and for SQL that runs past Django's
    cursor, which the guard does not see (the ``FETCH`` of a server-side
    cursor, see ``TenantQuerySet._scoped_chunks()``). Writes nothing when
    the session holds the values already, or with ``USE_LOCAL_SET=True`` in
    autocommit, where the guard gives each statement a transaction of its
    own. Opens the connection if needed.
    """
    connection.ensure_connection()
    tracker = _tracker_of(connection)
    if tracker is not None:
        _apply_if_stale(connection, tracker)


def settle_other_aliases(using: str | None, *, cleared: bool = False) -> None:
    """Take a context's values off this thread's other connections once the context has ended.

    Called when a context block exits: the guard may have applied the block's
    context on the other aliases in ``RLS_TENANTS["DATABASES"]``, and SQL the
    guard does not see (``callproc()``, a driver cursor) would find it there
    until their next statement. A connection that holds a context's values
    other than the active context's gets the active one's (or none) now;
    one that holds none is left alone, unless ``cleared``. With
    ``USE_LOCAL_SET=True`` that only happens in a transaction still open
    (``transaction.atomic()``, say): a transaction-local value is gone once
    its transaction has ended.

    Args:
        using: The alias the block restored itself, or ``None`` for a block
            that wrote nothing on entry (``USE_LOCAL_SET=True`` on a
            configured alias), so every configured alias is settled.
        cleared: The block held no context (``context._no_rls_state()``), so
            the guard cleared the GUCs of the connections it saw, which the
            tracker records as ``None``: a connection that holds none gets the
            active context's values too, where there is an active context.
    """
    for alias in rls_tenants_config.DATABASES:
        if alias == using:
            continue
        connection = _connection_of(alias)
        tracker = _tracker_of(connection)
        if connection is None or connection.connection is None or tracker is None:
            continue
        held = tracker.values
        # Unknown values in a transaction still open may be the block's own: a
        # block that wrote nothing has no restore to put the session right.
        unknown_in_transaction = (
            using is None and held is UNKNOWN and _observe(connection, tracker) != _IDLE
        )
        cleared_by_the_block = cleared and held is None
        if not isinstance(held, tuple) and not unknown_in_transaction and not cleared_by_the_block:
            continue
        try:
            _apply_if_stale(connection, tracker)
        except DatabaseError:
            # Not this block's connection: its own exit, or error, goes first.
            # The guard applies the context there before its next statement.
            tracker.forget()
            logger.debug(
                "GUC affinity guard: could not settle alias %r on exit",
                alias,
                exc_info=True,
            )


def suspect_sessions() -> None:
    """Treat what this thread's configured connections hold for the session as unknown.

    Called with ``USE_LOCAL_SET=True`` when a context ends (a block exits, a
    request ends). The library never writes the admin or tenant GUCs for the
    session in that mode, but code inside the context may have
    (``set_guc()``, ``rls_context()`` without ``is_local=True``), and such a
    value would outlast the context, unlike in session mode, where the
    context's exit restores the GUCs. The next statement run outside any
    context in autocommit clears them first. No statement runs here.
    """
    for alias in rls_tenants_config.DATABASES:
        connection = _connection_of(alias)
        tracker = _tracker_of(connection)
        if connection is None or connection.connection is None or tracker is None:
            continue
        try:
            _observe(connection, tracker)
        except Exception:
            # A connection that cannot report its status (a broken one): its
            # values are unknown either way. Called while unwinding a block,
            # so nothing is raised here.
            logger.debug("GUC affinity guard: could not read alias %r", alias, exc_info=True)
        tracker.suspect_session()


# ---- Following the SQL that runs ----


class _Statement(NamedTuple):
    """The start of a statement, as far as the tracker needs it."""

    command: str  # first word, upper-cased
    words: list[str]  # the first words, as written
    compound: bool  # more than one statement


def _refused_in_a_transaction(statement: _Statement | None) -> bool:
    """Whether PostgreSQL may refuse ``statement`` inside a transaction block.

    ``VACUUM``, ``CLUSTER``, ``REINDEX`` and ``DISCARD``; ``CREATE``, ``ALTER``
    or ``DROP`` of a database, a tablespace or a subscription, and
    ``ALTER SYSTEM``; ``CREATE [UNIQUE] INDEX CONCURRENTLY`` and
    ``DROP INDEX CONCURRENTLY``; ``... DETACH PARTITION name CONCURRENTLY``.
    Matched on the statement's first words (see ``_SQL_HEAD``) in that
    grammar, so a statement that only mentions those words elsewhere (in a
    comment, say) still gets a transaction. ``SET TRANSACTION`` never gets
    one either: it is transaction control (see :func:`_controls_the_transaction`).
    """
    if statement is None:
        return False
    command = statement.command.split("(", 1)[0]  # VACUUM(ANALYZE) t
    if command in _DDL_COMMANDS:
        return _refused_ddl(command, [word.rstrip(";").upper() for word in statement.words[1:]])
    return command in _NO_TRANSACTION_COMMANDS


def _controls_the_transaction(statement: _Statement | None) -> bool:
    """Whether ``statement`` only controls the transaction, so it reads no data.

    The commands in ``_TRANSACTION_CONTROL``, and ``SET TRANSACTION`` (also
    ``SET TRANSACTION SNAPSHOT``), which must be a transaction's first
    statement: the guard writes nothing before it. A compound statement is
    not one.
    """
    if statement is None or statement.compound:
        return False
    if statement.command in _TRANSACTION_CONTROL:
        return True
    following = [word.upper() for word in statement.words[1:2]]
    return statement.command == "SET" and following == ["TRANSACTION"]


def _refused_ddl(command: str, words: list[str]) -> bool:
    """The ``CREATE`` / ``ALTER`` / ``DROP`` part of :func:`_refused_in_a_transaction`.

    Args:
        command: The statement's command, upper-cased.
        words: The words after it, upper-cased, without a trailing semicolon.
    """
    if words[:1] and words[0] in _NO_TRANSACTION_OBJECTS:
        return True
    if command != "ALTER":
        return words[:2] == ["INDEX", "CONCURRENTLY"] or words[:3] == [
            "UNIQUE",
            "INDEX",
            "CONCURRENTLY",
        ]
    detach = words.index("DETACH") if "DETACH" in words else -1
    return detach >= 0 and words[detach + 1 : detach + 4 : 2] == ["PARTITION", "CONCURRENTLY"]


def _skip_leading_comments(sql: str) -> str:
    """Return ``sql`` without leading whitespace and ``--`` / ``/* */`` comments (nested)."""
    text = sql.lstrip()
    while text.startswith(("--", "/*")):
        if text.startswith("--"):
            end = text.find("\n")
            text = "" if end < 0 else text[end + 1 :].lstrip()
            continue
        depth, index = 1, 2
        while depth and index < len(text):
            if text.startswith("/*", index):
                depth, index = depth + 1, index + 2
            elif text.startswith("*/", index):
                depth, index = depth - 1, index + 2
            else:
                index += 1
        text = text[index:].lstrip()
    return text


def _parse(sql: object, cursor: Any = None) -> _Statement | None:  # Any: a driver cursor
    """Read the command of ``sql``; ``None`` if it cannot be read (not text, or only comments).

    A statement composed with ``psycopg2.sql`` / ``psycopg.sql`` is rendered
    with ``cursor``, the driver cursor it runs on; one that cannot be rendered
    is unreadable.
    """
    render = getattr(sql, "as_string", None)
    if render is not None:
        try:
            sql = render(cursor)
        except Exception:
            return None
    if isinstance(sql, bytes):
        sql = sql.decode("latin-1")  # only ASCII keywords are looked at
    if not isinstance(sql, str):
        return None
    text = _skip_leading_comments(sql[:_SQL_HEAD])
    if not text and len(sql) > _SQL_HEAD:
        # A leading comment that runs past the head: skip it on the whole text.
        text = _skip_leading_comments(sql)[:_SQL_HEAD]
    words = text.split()
    if not words:
        return None if sql.strip() else _Statement("", [], compound=False)
    semicolon = sql.find(";")
    compound = semicolon >= 0 and bool(sql[semicolon:].strip("; \t\r\n"))
    return _Statement(words[0].rstrip(";").upper(), words, compound)


def _identifier(word: str) -> str:
    """Return a SQL identifier as PostgreSQL compares it: quoted keeps its case."""
    word = word.rstrip(";")
    if len(word) > 1 and word[0] == word[-1] == '"':
        return word[1:-1].replace('""', '"')
    return word.translate(_ASCII_LOWER)


def _follow(
    statement: _Statement,
    status_before: int,
    connection: BaseDatabaseWrapper,
    tracker: _SessionGUCs,
) -> None:
    """Update ``tracker`` after a statement in ``_FOLLOWED`` has run.

    Args:
        statement: The statement, as ``_parse()`` read it.
        status_before: The session's transaction status before it ran.
        connection: The connection it ran on.
        tracker: The connection's tracker.
    """
    words = statement.words
    argument = _identifier(words[1]).upper() if len(words) > 1 else ""
    if statement.command in {"RESET", "DISCARD"}:
        conf = rls_tenants_config
        names = {conf.GUC_IS_ADMIN.upper(), conf.GUC_CURRENT_TENANT.upper()}
        if argument == "ALL" or (statement.command == "RESET" and argument in names):
            tracker.forget()
        return
    if argument == "PREPARED":  # two-phase commit: another transaction
        return
    rolls_back_to = statement.command == "ROLLBACK" and "TO" in (w.upper() for w in words)
    if statement.command in {"ROLLBACK", "ABORT"} and not rolls_back_to:
        tracker.end_transaction(committed=False)
    elif statement.command in {"COMMIT", "END"}:
        # PostgreSQL answers COMMIT in a failed transaction with ROLLBACK.
        tracker.end_transaction(committed=status_before != _IN_ERROR)
    elif statement.command == "PREPARE" and argument == "TRANSACTION":
        # The transaction is no longer this session's; what it leaves is unclear.
        tracker.end_transaction(committed=False)
        tracker.forget()
    # Picks up a transaction this statement opened or closed.
    _observe(connection, tracker)
    if statement.command == "SAVEPOINT":
        name = _savepoint_name(words, after="SAVEPOINT")
        if name is None:
            tracker.forget()
        else:
            tracker.savepoints.append((name, tracker.values))
    elif statement.command == "RELEASE" or rolls_back_to:
        name = _savepoint_name(words, after="TO" if rolls_back_to else "RELEASE")
        _follow_savepoint_end(name, tracker, rolled_back=rolls_back_to)


def _savepoint_name(words: list[str], *, after: str) -> str | None:
    """Return the savepoint named after the keyword ``after``, or ``None`` if unclear.

    ``SAVEPOINT name``, ``RELEASE [SAVEPOINT] name`` and
    ``ROLLBACK [WORK | TRANSACTION] TO [SAVEPOINT] name``. Anything after the
    name (a comment, say) makes it unclear; a semicolon right after it does not.
    """
    upper = [word.upper() for word in words]
    index = upper.index(after) + 1
    if after != "SAVEPOINT" and index < len(words) and upper[index] == "SAVEPOINT":
        index += 1
    if index != len(words) - 1:
        return None
    return _identifier(words[index])


def _follow_savepoint_end(name: str | None, tracker: _SessionGUCs, *, rolled_back: bool) -> None:
    """Follow ``RELEASE`` or ``ROLLBACK TO`` of the savepoint ``name`` (``None``: unclear)."""
    if name is None:
        tracker.forget()
        return
    index = tracker.savepoint_index(name)
    if index is None:
        # A savepoint set where the guard could not see it: nothing to go back to.
        if tracker.changed:
            tracker.forget()
    elif rolled_back:
        # The savepoint stays after a rollback to it.
        tracker.values = tracker.savepoints[index][1]
        del tracker.savepoints[index + 1 :]
    else:
        del tracker.savepoints[index:]


# ---- A transaction of its own (USE_LOCAL_SET=True) ----


def _needs_own_transaction(
    connection: BaseDatabaseWrapper,
    status: int,
    statement: _Statement | None,
) -> bool:
    """Whether the guard runs a statement in a transaction of its own.

    With ``USE_LOCAL_SET=True``, a statement that would run in autocommit
    while a context is active, on an alias in ``RLS_TENANTS["DATABASES"]``:
    the context's values are transaction-local, so the statement needs a
    transaction to be set in. Not a statement PostgreSQL may refuse in one.

    Args:
        connection: The connection the statement runs on.
        status: The session's transaction status.
        statement: The statement, as ``_parse()`` read it.
    """
    # The guard runs this for every statement: the cheap tests first. The first
    # and third are ``not _will_run_in_transaction()``.
    return (
        status == _IDLE
        and rls_tenants_config.USE_LOCAL_SET
        and connection.autocommit
        and _get_context_gucs() is not None
        and _is_guarded(connection)
        and not _refused_in_a_transaction(statement)
    )


def _send_transaction_control(
    connection: BaseDatabaseWrapper,
    command: Literal["BEGIN", "COMMIT", "ROLLBACK"],
) -> None:
    """Send ``BEGIN``, ``COMMIT`` or ``ROLLBACK`` on ``connection``, past Django's cursor.

    With a driver cursor, so no execute wrapper sees it, and logged as Django
    logs its own transaction control (``debug_transaction``): in
    ``connection.queries`` when queries are logged, and on the
    ``django.db.backends`` logger.

    Raises:
        DatabaseError: Raised as Django's database exceptions.
    """
    with (
        debug_transaction(connection, command),
        connection.wrap_database_errors,
        connection.connection.cursor() as cursor,
    ):
        cursor.execute(command)


def _run_in_own_transaction(
    execute: Callable[[str, Any, bool, dict[str, Any]], Any],
    sql: str,
    params: Any,  # Any: Django's execute-wrapper signature
    many: bool,
    context: dict[str, Any],
    tracker: _SessionGUCs,
) -> Any:
    """Run an autocommit statement in a transaction of its own, with the context's values.

    ``BEGIN``; both values, transaction-locally, in one ``set_config()``
    statement through Django's cursor (like the guard's other writes); the
    statement; ``COMMIT``. The values end with the transaction, so the
    session never holds them, and the statement still commits, or fails and
    is rolled back, on its own, as in autocommit (an ``executemany()`` as a
    whole). A server-side cursor it declares (Django's are ``WITH HOLD`` in
    autocommit) is computed at the ``COMMIT``, while the values are still
    set, as it would be at the end of its own implicit transaction.

    Returns:
        What ``execute`` returned.

    Raises:
        BaseException: Whatever the statement, the write of the values or
            the ``COMMIT`` raised, after the transaction is rolled back.
    """
    connection: BaseDatabaseWrapper = context["connection"]
    values = _get_context_gucs()
    try:
        _send_transaction_control(connection, "BEGIN")
        _observe(connection, tracker)
        write_gucs(connection, values, is_local=True)
        result = execute(sql, params, many, context)
        _send_transaction_control(connection, "COMMIT")
    except BaseException:
        _abandon_own_transaction(connection, tracker)
        raise
    tracker.end_transaction(committed=True)
    return result


def _abandon_own_transaction(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> None:
    """Roll back the guard's own transaction after a failure, which then propagates.

    A failed ``COMMIT`` has ended it already. When the rollback cannot be
    confirmed (a broken connection, a second failure, an interruption), the
    connection is closed: Django believes it is in autocommit, and later
    statements must not run inside a transaction left open. A pool rolls back
    a connection it gets back; the next statement opens a new one.
    """
    ended = False
    try:
        if tracker.read_status() != _IDLE:
            _send_transaction_control(connection, "ROLLBACK")
        ended = tracker.read_status() == _IDLE
    except Exception:
        # The statement's own error is the one that propagates.
        logger.debug("GUC affinity guard: could not roll back its transaction", exc_info=True)
    finally:
        if ended:
            tracker.end_transaction(committed=False)
        else:
            tracker.forget()
            try:
                connection.close()
            except Exception:
                logger.debug("GUC affinity guard: could not close the connection", exc_info=True)


# ---- The execute wrapper ----


def _guard(
    execute: Callable[[str, Any, bool, dict[str, Any]], Any],
    sql: str,
    params: Any,  # Any: Django's execute-wrapper signature
    many: bool,
    context: dict[str, Any],
) -> Any:
    """Execute wrapper: apply the active context's GUCs before the statement if needed."""
    connection: BaseDatabaseWrapper = context["connection"]
    tracker = _tracker_of(connection)
    if tracker is None:
        return execute(sql, params, many, context)
    # context["cursor"] is Django's CursorWrapper; its .cursor is the driver's.
    statement = _parse(sql, getattr(context.get("cursor"), "cursor", None))
    control = _controls_the_transaction(statement)
    own_transaction = False
    if _writes.depth or control:
        # The library's own write, or no data read: only follow the transaction.
        status = _observe(connection, tracker)
    else:
        status = _apply_if_stale(connection, tracker)
        own_transaction = _needs_own_transaction(connection, status, statement)
    try:
        if own_transaction:
            result = _run_in_own_transaction(execute, sql, params, many, context, tracker)
        else:
            result = execute(sql, params, many, context)
    except BaseException:
        if statement is None or statement.command in _FOLLOWED:
            # A failed transaction-control statement leaves the GUCs undetermined.
            tracker.forget()
        raise
    if statement is None or statement.compound:
        # It may have changed the transaction or the GUCs in ways not followed.
        tracker.forget()
    elif statement.command in _FOLLOWED:
        _follow(statement, status, connection, tracker)
    if own_transaction:
        # A statement run under a context's transaction-local values may have
        # written the GUCs for the session (set_guc(), rls_context()): on this
        # connection, whichever thread runs it, they count as unknown now.
        tracker.suspect_session()
    elif tracker.wrote_local and tracker.in_transaction and not _writes.depth:
        # The same in a transaction holding local values: unknown after a commit.
        tracker.suspect_session()
    return result
