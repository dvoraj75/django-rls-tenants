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

The library's own GUC writes run inside :func:`writing`, which keeps the guard
out of the way and records the values they leave. Writes of the same GUCs by
other code (``set_guc()``, ``rls_context()``) are not recorded, so they stay
in force until the context changes, as before.

PostgreSQL undoes a ``set_config()`` made in a transaction or savepoint that
is rolled back. The tracker follows the transaction status the driver reports
(libpq, without a round trip), Django's commit and rollback of the connection
(``install()`` wraps them), and the transaction-control statements that run
through Django's cursor: a savepoint keeps the values it was set with, and a
``ROLLBACK TO`` it puts them back. Whatever it cannot follow (a transaction a
driver cursor ended, a savepoint it did not see set, a statement it cannot
read) makes the values unknown, and the next statement applies the context's
values again.

Limits: the guard only works with session GUCs; with ``USE_LOCAL_SET=True`` it
does nothing and never writes a session GUC. It covers the admin and tenant
GUCs, not bypass flags or other GUCs set with ``rls_context()``. It cannot
see SQL that bypasses Django's execute wrappers (a driver cursor,
``CursorWrapper.callproc()``, the driver's ``copy`` methods, the ``FETCH`` of a
server-side cursor that is already open); on a connection the guard has used,
such SQL sees whatever values it left there until the next statement it sees.
"""

from __future__ import annotations

import logging
import string
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from django.db import DatabaseError, connections
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

    def attach(self, raw: Any, values: _Tracked) -> None:  # Any: a DB-API connection
        """Start tracking a new session, ``raw``, whose GUCs hold ``values``."""
        self.raw = raw
        self.read_status = _status_reader(raw)
        self.values = values
        self.in_transaction = False
        self.base = values
        self.changed = False
        self.savepoints.clear()

    def sync_transaction(self) -> int:
        """Bring the record up to date with the session's transaction status, and return it.

        A transaction that has ended unseen since the last look either
        committed the values recorded during it or rolled back to ``base``.
        They are kept only when the two agree.
        """
        status = self.read_status()
        if status == _IDLE:
            if self.in_transaction:
                self.in_transaction = False
                self.savepoints.clear()
                if self.values != self.base:
                    self.values = UNKNOWN
        elif not self.in_transaction:
            self.in_transaction = True
            self.base = self.values
            self.changed = False
            self.savepoints.clear()
        return status

    def set_values(self, values: _Tracked) -> None:
        """Record ``values`` as what the session holds."""
        if values != self.values:
            self.values = values
            if self.in_transaction:
                self.changed = True

    def forget(self) -> None:
        """Record that the values are not known."""
        self.set_values(UNKNOWN)

    def end_transaction(self, *, committed: bool | None) -> None:
        """Follow the end of the transaction: a commit keeps the values, a rollback undoes them.

        Args:
            committed: Whether the transaction committed; ``None`` if that is
                not known, which keeps the values only when both agree.
        """
        if self.in_transaction:
            if committed is None:
                if self.values != self.base:
                    self.values = UNKNOWN
            elif not committed:
                self.values = self.base
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
    """Whether the guard applies the context's GUCs on ``connection`` (session GUC mode only)."""
    return (
        not rls_tenants_config.USE_LOCAL_SET and connection.alias in rls_tenants_config.DATABASES
    )


def _observe(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> int:
    """Bring ``tracker`` up to date with ``connection``'s session, and return its status."""
    raw = connection.connection
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


def _record(target: BaseDatabaseWrapper | str, values: _Tracked) -> None:
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
        values: The values, ``None`` for cleared GUCs, or ``UNKNOWN``.
    """
    connection = _connection_of(target)
    if connection is None or connection.connection is None:
        return
    tracker = _tracker_of(connection)
    if tracker is None:
        install(connection, values=UNKNOWN)
        return
    _observe(connection, tracker)
    tracker.set_values(values if _is_guarded(connection) else UNKNOWN)


def _forget(target: BaseDatabaseWrapper | str) -> None:
    """Mark a connection's GUC values unknown, without touching the driver."""
    tracker = _tracker_of(_connection_of(target))
    if tracker is not None:
        tracker.forget()


@contextmanager
def writing(target: BaseDatabaseWrapper | str, values: _Tracked) -> Iterator[None]:
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
    """
    depth = _writes.depth
    _writes.depth = depth + 1
    try:
        yield
    except BaseException:
        _forget(target)
        raise
    else:
        _record(target, values)
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
        is_local: Set them transaction-locally (``SET LOCAL`` semantics); only
            the ``connection_created`` handler does, with ``USE_LOCAL_SET=True``.

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


# ---- Applying the active context ----


def _apply_if_stale(connection: BaseDatabaseWrapper, tracker: _SessionGUCs) -> int:
    """Apply the active context's GUC values on ``connection`` if its session holds others.

    Skipped where a statement cannot run anyway (a failed transaction or
    connection, or a transaction Django has marked for rollback), so that
    statement's own error surfaces.

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
    write_gucs(connection, context_gucs)
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
    the guard stays out of the way. Opens the connection if needed.
    """
    connection.ensure_connection()
    tracker = _tracker_of(connection)
    if tracker is not None:
        _apply_if_stale(connection, tracker)


def settle_other_aliases(using: str) -> None:
    """Take a context's values off this thread's other connections once the context has ended.

    Called when a context block exits: the guard may have applied the block's
    context on the other aliases in ``RLS_TENANTS["DATABASES"]``, and SQL the
    guard does not see (``callproc()``, a driver cursor) would find it there
    until their next statement. A connection that holds a context's values
    other than the active context's gets the active one's (or none) now;
    one that holds none is left alone.

    Args:
        using: The alias the block restored itself.
    """
    for alias in rls_tenants_config.DATABASES:
        if alias == using:
            continue
        connection = _connection_of(alias)
        tracker = _tracker_of(connection)
        if (
            connection is None
            or connection.connection is None
            or tracker is None
            or not isinstance(tracker.values, tuple)
        ):
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


# ---- Following the SQL that runs ----


class _Statement(NamedTuple):
    """The start of a statement, as far as the tracker needs it."""

    command: str  # first word, upper-cased
    words: list[str]  # the first words, as written
    compound: bool  # more than one statement


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


def _parse(sql: object) -> _Statement | None:
    """Read the command of ``sql``; ``None`` if it cannot be read (not text, or only comments)."""
    if isinstance(sql, bytes):
        sql = sql.decode("latin-1")  # only ASCII keywords are looked at
    if not isinstance(sql, str):
        return None
    words = _skip_leading_comments(sql[:_SQL_HEAD]).split()
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
    statement = _parse(sql)
    control = (
        statement is not None
        and statement.command in _TRANSACTION_CONTROL
        and not statement.compound
    )
    if _writes.depth or control:
        # The library's own write, or no data read: only follow the transaction.
        status = _observe(connection, tracker)
    else:
        status = _apply_if_stale(connection, tracker)
    try:
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
    return result
