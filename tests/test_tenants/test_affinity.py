"""Tests for django_rls_tenants.tenants.affinity (the GUC affinity guard, #41)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings as django_settings
from django.contrib.auth.models import AnonymousUser
from django.core.signals import got_request_exception, request_finished
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.backends.postgresql.psycopg_any import sql as pg_sql
from django.db.transaction import TransactionManagementError
from django.http import HttpRequest
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.rls.context import rls_context
from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants import affinity
from django_rls_tenants.tenants import middleware as middleware_module
from django_rls_tenants.tenants.context import (
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
)
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware
from django_rls_tenants.tenants.state import (
    ADMIN_GUCS,
    ContextGucs,
    _clear_context_gucs,
    _get_context_gucs,
    _hold_context_gucs,
    _reset_context_gucs,
    _set_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)
from tests.session_gucs import session_gucs
from tests.test_app.models import Order

pytestmark = pytest.mark.django_db

_ALL_PRODUCTS = ["Gadget B1", "Widget A1", "Widget A2"]
_TENANT_A_PRODUCTS = ["Widget A1", "Widget A2"]
_NO_GUCS = (None, None)
_ADMIN_READ = (None, "true")


class _Failed(Exception):  # noqa: N818  -- a marker, not an error the code under test raises
    """Raised inside a block to see how it handles an exception."""


def _in_atomic(func, *, roll_back):
    """Call ``func`` in ``transaction.atomic()``; roll the block back or commit it.

    Returns:
        What ``func`` returned.
    """
    with transaction.atomic():
        result = func()
        transaction.set_rollback(roll_back)
    return result


def _gucs(using=DEFAULT_DB_ALIAS):
    """``(current_tenant, is_admin)``, read with a Django cursor, so after the guard."""
    return get_guc("rls.current_tenant", using=using), get_guc("rls.is_admin", using=using)


def _products(using=DEFAULT_DB_ALIAS):
    """The products the connection's RLS policy lets through: raw SQL, so no ORM filter."""
    conn = connections[using]
    with conn.cursor() as cursor:
        table = conn.ops.quote_name(Order._meta.db_table)
        cursor.execute(f"SELECT product FROM {table} ORDER BY product")
        return [row[0] for row in cursor.fetchall()]


def _read_as(tenant):
    """What ``_gucs()`` and ``session_gucs()`` read in the tenant's context."""
    return str(tenant.pk), "false"


@contextmanager
def _context(values):
    """Make ``values`` the active context's, as another task's context would be.

    Only the guard's ``ContextVar``: no GUC is written, so the connection holds
    whatever it held before, as a sibling task's shared connection does.
    """
    token = _set_context_gucs(values)
    try:
        yield
    finally:
        _reset_context_gucs(token)


def _context_of(tenant):
    """``_context()`` with the tenant's values."""
    return _context(ContextGucs.for_tenant(tenant.pk))


def _tracker(conn=connection):
    """The guard's record of a connection's session."""
    return getattr(conn, affinity._TRACKER_ATTR)


def _guard_statements(queries):
    """The guard's own statements among captured ones: it sets both GUCs in one.

    Django 5.2 logs a statement that never reached the driver with ``sql=None``.
    """
    return [query["sql"] for query in queries if (query["sql"] or "").count("set_config(") > 1]


def _request_for(user):
    request = HttpRequest()
    request.user = user
    return request


@pytest.fixture
def worker():
    """One worker thread, like the executor that runs ``thread_sensitive=False`` hops.

    Its connection lives across jobs, as a server's worker threads keep theirs.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor
        executor.submit(connections.close_all).result()


def _hop(worker, func, context=None):
    """Run ``func`` on ``worker`` in a copy of the current context, like a hop.

    ``context`` replaces that copy, e.g. an empty ``Context`` for work done
    outside any request.
    """
    run_in = contextvars.copy_context() if context is None else context
    return worker.submit(run_in.run, func).result()


@pytest.mark.django_db(transaction=True)
class TestHops:
    """A ``thread_sensitive=False`` hop: a copy of the context on another thread's connection.

    ``transaction=True``: the worker's connection sees committed rows only.
    """

    def test_hop_inherits_a_tenant_context(
        self,
        sample_orders,
        tenant_a,
        worker,
        enforce_rls_on_other_threads,
    ):
        with tenant_context(tenant_a.pk):
            gucs, products = _hop(worker, lambda: (_gucs(), _products()))

        assert (gucs, products) == (_read_as(tenant_a), _TENANT_A_PRODUCTS)

    def test_hop_inherits_an_admin_context(
        self,
        sample_orders,
        worker,
        enforce_rls_on_other_threads,
    ):
        with admin_context():
            gucs, products = _hop(worker, lambda: (_gucs(), _products()))

        assert (gucs, products) == (_ADMIN_READ, _ALL_PRODUCTS)

    def test_hop_without_a_context_loses_what_an_earlier_hop_left(
        self,
        sample_orders,
        tenant_a,
        worker,
        enforce_rls_on_other_threads,
    ):
        """A later job on the same thread and connection, outside any context, sees nothing."""
        with tenant_context(tenant_a.pk):
            assert _hop(worker, _products) == _TENANT_A_PRODUCTS

        session, gucs, products = _hop(
            worker,
            lambda: (session_gucs(), _gucs(), _products()),
            contextvars.Context(),
        )

        assert session == _read_as(tenant_a)  # still on the session ...
        assert (gucs, products) == (_NO_GUCS, [])  # ... until the next statement

    def test_worker_connection_follows_each_hops_context(
        self,
        sample_orders,
        tenant_a,
        tenant_b,
        worker,
        enforce_rls_on_other_threads,
    ):
        with tenant_context(tenant_a.pk):
            first = _hop(worker, _products)
        with tenant_context(tenant_b.pk):
            second = _hop(worker, _products)
        with admin_context():
            third = _hop(worker, _products)

        assert (first, second, third) == (_TENANT_A_PRODUCTS, ["Gadget B1"], _ALL_PRODUCTS)

    def test_copy_that_outlives_the_block_runs_without_its_context(
        self,
        sample_orders,
        worker,
        enforce_rls_on_other_threads,
    ):
        """Work started inside a block and run after it keeps no rights (fail-closed)."""
        with admin_context():
            escaped = contextvars.copy_context()

        assert _hop(worker, _products, escaped) == []

    def test_new_session_on_the_same_connection_object_gets_the_context(self, tenant_a):
        """A reconnect replaces the session; the guard tracks the new one from scratch."""
        with tenant_context(tenant_a.pk):
            connection.close()
            connection.ensure_connection()
            assert session_gucs() == _NO_GUCS
            assert _gucs() == _read_as(tenant_a)


async def _interleaved(first_block, second_block):
    """Run two sibling tasks that share the connection, interleaved as gather allows.

    The first task enters its block, then the second enters its own, which sets
    the GUCs on the shared connection last. Then the first task queries, and
    then the second. Each returns its products.
    """
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    first_read = asyncio.Event()

    async def first():
        async with first_block:
            first_entered.set()
            await second_entered.wait()
            products = await sync_to_async(_products, thread_sensitive=True)()
            first_read.set()
            return products

    async def second():
        await first_entered.wait()
        async with second_block:
            second_entered.set()
            await first_read.wait()
            return await sync_to_async(_products, thread_sensitive=True)()

    return await asyncio.gather(first(), second())


async def _task_that_outlives_its_block():
    """Start a task inside an admin block and let it query after the block has ended."""
    block_ended = asyncio.Event()

    async def later():
        await block_ended.wait()
        return await sync_to_async(_products, thread_sensitive=True)()

    async with async_admin_context():
        task = asyncio.ensure_future(later())  # copies the block's context
    block_ended.set()
    return await task


class TestSiblingTasks:
    """Tasks of one request share its connection but not its ``ContextVar`` values."""

    def test_each_task_queries_its_own_tenant(
        self, sample_orders, tenant_a, tenant_b, enforce_rls
    ):
        first, second = async_to_sync(_interleaved)(
            async_tenant_context(tenant_a.pk),
            async_tenant_context(tenant_b.pk),
        )

        assert (first, second) == (_TENANT_A_PRODUCTS, ["Gadget B1"])
        assert _gucs() == _NO_GUCS
        assert _products() == []

    def test_an_admin_sibling_does_not_widen_a_tenant_task(
        self,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        """Raw SQL has no ORM filter, so only the GUCs keep tenant A's task to its rows."""
        first, second = async_to_sync(_interleaved)(
            async_tenant_context(tenant_a.pk),
            async_admin_context(),
        )

        assert (first, second) == (_TENANT_A_PRODUCTS, _ALL_PRODUCTS)
        assert _gucs() == _NO_GUCS

    def test_task_that_outlives_its_block_runs_without_its_context(
        self,
        sample_orders,
        enforce_rls,
    ):
        assert async_to_sync(_task_that_outlives_its_block)() == []


class TestSavepointRollback:
    """``ROLLBACK TO SAVEPOINT`` undoes the ``set_config()`` calls made after the savepoint."""

    def test_guard_applies_the_context_again(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            # The SAVEPOINT statement is not reconciled, so tenant B is applied
            # inside the savepoint, and undone with it.
            assert _in_atomic(_gucs, roll_back=True) == _read_as(tenant_b)

            assert session_gucs() == _read_as(tenant_a)
            assert _gucs() == _read_as(tenant_b)

    def test_rollback_to_a_savepoint_restores_the_values_it_saw(self, tenant_a, tenant_b):
        """Nested savepoints: the inner rollback brings back exactly what the outer one set."""

        def apply_admin():
            with _context(ADMIN_GUCS):
                _gucs()  # admin applied in the inner savepoint

        def outer():
            _gucs()  # tenant B applied in the outer savepoint
            _in_atomic(apply_admin, roll_back=True)
            with CaptureQueriesContext(connection) as queries:
                gucs = _gucs()
            return gucs, _guard_statements(queries), session_gucs()

        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            gucs, guard_statements, session = _in_atomic(outer, roll_back=True)

        assert gucs == session == _read_as(tenant_b)
        assert guard_statements == []

    def test_rollback_without_a_change_needs_no_statement(self, tenant_a):
        with tenant_context(tenant_a.pk):
            _in_atomic(lambda: list(Order.objects.all()), roll_back=True)
            with CaptureQueriesContext(connection) as queries:
                assert get_guc("rls.current_tenant") == str(tenant_a.pk)
        assert len(queries) == 1

    def test_savepoint_statements_are_not_reconciled(self, tenant_a):
        """The context is applied before the first data statement, inside the savepoint."""
        with (
            _context_of(tenant_a),
            CaptureQueriesContext(connection) as queries,
            transaction.atomic(),
        ):
            pass

        assert [query["sql"].split()[0] for query in queries] == ["SAVEPOINT", "RELEASE"]

    def test_broken_transaction_gets_no_guard_statement(self, tenant_a):
        """Django refuses the statement itself; the guard does not try first."""

        def query_in_broken_transaction():
            transaction.set_rollback(True)
            with (
                _context_of(tenant_a),
                CaptureQueriesContext(connection) as queries,
                pytest.raises(TransactionManagementError),
            ):
                list(Order.objects.all())
            return _guard_statements(queries)

        assert _in_atomic(query_in_broken_transaction, roll_back=True) == []


@pytest.mark.django_db(transaction=True)
class TestTransactionEnd:
    """Django's commit and rollback of the connection tell the guard what the session holds."""

    @pytest.mark.parametrize("roll_back", [False, True], ids=["commit", "rollback"])
    def test_guard_applies_the_context_after_the_transaction(self, roll_back, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk), _context_of(tenant_b):  # tenant A committed
            assert _in_atomic(_gucs, roll_back=roll_back) == _read_as(tenant_b)
            session = session_gucs()

            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == _read_as(tenant_b)

        # A commit kept tenant B, so nothing to apply; a rollback brought back A.
        assert session == _read_as(tenant_a if roll_back else tenant_b)
        assert len(_guard_statements(queries)) == int(roll_back)

    def test_commit_after_the_context_changed_back(self, tenant_a, tenant_b):
        """The session keeps the committed values although the context moved on unseen."""

        def apply_tenant_b():
            with _context_of(tenant_b):
                _gucs()

        with tenant_context(tenant_a.pk):
            _in_atomic(apply_tenant_b, roll_back=False)

            assert session_gucs() == _read_as(tenant_b)
            assert _gucs() == _read_as(tenant_a)

    def test_transaction_started_where_the_guard_cannot_see_it(self, tenant_a, tenant_b):
        """After a rollback, the next transaction opened by a driver cursor is no blind spot."""

        def through_a_driver_cursor_first():
            with connection.connection.cursor() as driver_cursor:
                driver_cursor.execute("SELECT 1")
            return _gucs()

        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            _in_atomic(_gucs, roll_back=True)  # tenant B applied, then rolled back

            assert _in_atomic(through_a_driver_cursor_first, roll_back=True) == _read_as(tenant_b)

    @pytest.mark.parametrize("roll_back", [False, True], ids=["commit", "rollback"])
    def test_balanced_transaction_needs_no_statement(self, roll_back, tenant_a):
        """A context entered and left inside a transaction leaves what was there before it."""

        def in_context():
            with tenant_context(tenant_a.pk):
                return _gucs()

        assert _in_atomic(in_context, roll_back=roll_back) == _read_as(tenant_a)
        with CaptureQueriesContext(connection) as queries:
            assert get_guc("rls.current_tenant") is None
        assert len(queries) == 1

    def test_context_after_a_transaction_needs_no_extra_statement(self, tenant_a):
        _in_atomic(_gucs, roll_back=False)

        with CaptureQueriesContext(connection) as queries, tenant_context(tenant_a.pk):
            _gucs()

        assert _guard_statements(queries) == []

    def test_transaction_the_driver_ended(self, tenant_a, tenant_b):
        """A commit Django did not make: the guard cannot tell it from a rollback."""
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            connection.set_autocommit(False)
            try:
                _gucs()  # tenant B applied in the transaction
                connection.connection.commit()
                with CaptureQueriesContext(connection) as queries:
                    assert _gucs() == _read_as(tenant_b)
            finally:
                connection.connection.rollback()
                connection.set_autocommit(True)

        assert len(_guard_statements(queries)) == 1

    def test_failed_commit_is_followed_by_the_rollback(self, tenant_a, tenant_b):
        """A deferred constraint fails the COMMIT; Django rolls back, and the guard follows."""

        def insert_an_orphan_order():
            with _context(ADMIN_GUCS):
                Order.objects.create(product="orphan", amount=Decimal(1), tenant_id=2**31 - 1)

        with tenant_context(tenant_a.pk):
            with pytest.raises(IntegrityError):
                _in_atomic(insert_an_orphan_order, roll_back=False)

            assert _tracker().values == ContextGucs.for_tenant(tenant_a.pk)
            assert session_gucs() == _read_as(tenant_a)
            with _context_of(tenant_b):
                assert _gucs() == _read_as(tenant_b)

    def test_local_writes_end_with_the_transaction(self):
        """``write_gucs(is_local=True)``, for USE_LOCAL_SET on a new connection."""

        def write_locally():
            affinity.write_gucs(connection, ADMIN_GUCS, is_local=True)
            return session_gucs()

        assert _in_atomic(write_locally, roll_back=False) == _ADMIN_READ
        assert session_gucs() == _NO_GUCS


@pytest.mark.django_db(transaction=True)
class TestTransactionControlInSql:
    """``BEGIN`` / ``ROLLBACK`` / ``COMMIT`` sent as SQL, which Django does not know about."""

    @staticmethod
    def _apply_in_sql_transaction(tenant_b, *, end, fail=False):
        """Apply tenant B inside a transaction opened with SQL, then end it with ``end``."""
        with connection.cursor() as cursor:
            cursor.execute("BEGIN")
            assert _gucs() == _read_as(tenant_b)
            if fail:
                with pytest.raises(DatabaseError):
                    cursor.execute("SELECT 1 / 0")
            cursor.execute(end)

    @pytest.mark.parametrize("end", ["ROLLBACK", "ABORT", "/* why */ ROLLBACK"])
    def test_rollback_brings_back_the_values_from_before(self, end, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk):  # committed in autocommit
            with _context_of(tenant_b):
                self._apply_in_sql_transaction(tenant_b, end=end)
            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == session_gucs() == _read_as(tenant_a)
        assert _guard_statements(queries) == []  # known to be tenant A again

    @pytest.mark.parametrize("end", ["COMMIT", "END", "commit;"])
    def test_commit_keeps_the_values(self, end, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            self._apply_in_sql_transaction(tenant_b, end=end)
            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == session_gucs() == _read_as(tenant_b)
        assert _guard_statements(queries) == []

    def test_commit_of_a_failed_transaction_is_a_rollback(self, tenant_a, tenant_b):
        """PostgreSQL answers COMMIT with ROLLBACK there; tenant A's values come back."""
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            self._apply_in_sql_transaction(tenant_b, end="COMMIT", fail=True)

            assert session_gucs() == _read_as(tenant_a)
            assert _gucs() == _read_as(tenant_b)


class TestStatementsTheGuardReads:
    """What the guard makes of statement text it did not write."""

    def test_rollback_after_a_comment_is_followed(self, tenant_a, tenant_b):
        def roll_back_after_a_comment():
            with _context_of(tenant_b), connection.cursor() as cursor:
                cursor.execute("SAVEPOINT commented")
                _gucs()  # tenant B applied after the savepoint
                cursor.execute("-- undo\n/* the /* nested */ part */ ROLLBACK TO commented")
                return session_gucs(), _gucs()

        with tenant_context(tenant_a.pk):
            session, gucs = _in_atomic(roll_back_after_a_comment, roll_back=True)

        assert (session, gucs) == (_read_as(tenant_a), _read_as(tenant_b))

    def test_statement_after_a_transaction_control_one_is_reconciled(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            with connection.cursor() as cursor:
                cursor.execute("SAVEPOINT compound; SELECT 1")
            assert session_gucs() == _read_as(tenant_b)

    def test_rollback_to_an_unseen_savepoint_makes_the_values_unknown(
        self,
        tenant_a,
        tenant_b,
        caplog,
    ):
        """A savepoint set with a driver cursor: the guard applies the context again."""

        def roll_back_to_a_driver_savepoint():
            with connection.connection.cursor() as driver_cursor:
                driver_cursor.execute("SAVEPOINT driver_sp")
            with _context_of(tenant_b):
                _gucs()  # tenant B applied after the savepoint
                with connection.cursor() as cursor:
                    cursor.execute("ROLLBACK TO SAVEPOINT Driver_SP")  # unquoted: folded
                with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
                    return session_gucs(), _gucs()

        with tenant_context(tenant_a.pk):
            session, gucs = _in_atomic(roll_back_to_a_driver_savepoint, roll_back=True)

        assert session == _read_as(tenant_a)
        assert gucs == _read_as(tenant_b)
        assert any("held <unknown>" in message for message in caplog.messages)

    @pytest.mark.django_db(transaction=True)
    def test_rollback_to_an_unseen_savepoint_without_a_change_needs_no_statement(self, tenant_a):
        """Nothing changed in the transaction (the context was entered before it)."""

        def roll_back_to_a_driver_savepoint():
            with connection.connection.cursor() as driver_cursor:
                driver_cursor.execute("SAVEPOINT driver_sp")
            with connection.cursor() as cursor:
                cursor.execute("ROLLBACK TO SAVEPOINT driver_sp")
            with CaptureQueriesContext(connection) as queries:
                _gucs()
            return _guard_statements(queries)

        with tenant_context(tenant_a.pk):
            assert _in_atomic(roll_back_to_a_driver_savepoint, roll_back=True) == []

    def test_failed_savepoint_statement_makes_the_values_unknown(self):
        def fail():
            with pytest.raises(DatabaseError), connection.cursor() as cursor:
                cursor.execute('ROLLBACK TO SAVEPOINT "no_such_savepoint"')
            return _tracker().values

        assert _in_atomic(fail, roll_back=True) is affinity.UNKNOWN

    def test_prepared_statement_is_not_a_prepared_transaction(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute("PREPARE rls_probe AS SELECT 1")
                cursor.execute("DEALLOCATE rls_probe")
            assert _tracker().values == ContextGucs.for_tenant(tenant_a.pk)

    @pytest.mark.parametrize("sql", ["COMMIT PREPARED 'tx'", "ROLLBACK PREPARED 'tx'"])
    def test_two_phase_commit_of_another_transaction_changes_nothing(self, sql):
        tracker = affinity._SessionGUCs()
        tracker.attach(connection.connection, ContextGucs.for_tenant(7))
        tracker.in_transaction = True
        tracker.base = ContextGucs.for_tenant(1)

        affinity._follow(affinity._parse(sql), affinity._IDLE, connection, tracker)

        assert tracker.values == ContextGucs.for_tenant(7)

    def test_prepared_transaction_makes_the_values_unknown(self):
        tracker = affinity._SessionGUCs()
        tracker.attach(connection.connection, ContextGucs.for_tenant(7))
        tracker.in_transaction = True

        affinity._follow(
            affinity._parse("PREPARE TRANSACTION 'tx'"),
            affinity._IDLE,
            connection,
            tracker,
        )

        assert tracker.values is affinity.UNKNOWN

    @pytest.mark.parametrize(
        ("sql", "command", "compound"),
        [
            ("  select 1", "SELECT", False),
            ("COMMIT;", "COMMIT", False),
            ("SELECT 1;  \n", "SELECT", False),
            ("BEGIN; SELECT 1", "BEGIN", True),
            ("/* a /* nested */ comment */ -- and a line\nRELEASE x", "RELEASE", False),
            (b"ROLLBACK", "ROLLBACK", False),
            ("", "", False),
        ],
    )
    def test_parse(self, sql, command, compound):
        statement = affinity._parse(sql)

        assert (statement.command, statement.compound) == (command, compound)

    @pytest.mark.parametrize("sql", [object(), "/* only a comment */", "-- only a comment"])
    def test_parse_gives_up_on_what_it_cannot_read(self, sql):
        assert affinity._parse(sql) is None

    @pytest.mark.parametrize(
        ("word", "name"),
        [("Foo", "foo"), ('"Foo"', "Foo"), ('"a""b"', 'a"b'), ("É", "É"), ("x;", "x")],
    )
    def test_identifier_folds_like_postgresql(self, word, name):
        assert affinity._identifier(word) == name


class TestSessionReset:
    @pytest.mark.parametrize(
        "statement",
        ["RESET rls.current_tenant;", 'RESET "rls.current_tenant"', "RESET ALL"],
    )
    def test_reset_makes_the_guard_apply_the_context_again(self, statement, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute(statement)
            assert _gucs() == _read_as(tenant_a)

    def test_reset_of_another_setting_needs_no_statement(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute("RESET statement_timeout")
            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == _read_as(tenant_a)
        assert len(queries) == 2


class TestWhatTheGuardLeavesAlone:
    def test_public_state_functions_do_not_drive_the_gucs(self):
        """Only the library's own contexts are applied: no admin inferred from the active flag."""
        set_rls_context_active(True)
        set_current_tenant_id(None)
        set_guc("rls.current_tenant", "5")

        assert _gucs() == ("5", None)

    def test_manual_write_inside_a_context_stays(self, tenant_a):
        with tenant_context(tenant_a.pk), rls_context({"rls.current_tenant": "999"}):
            assert _gucs() == ("999", "false")
        assert _gucs() == _NO_GUCS

    def test_alias_outside_rls_databases_does_not_follow(self, replica, tenant_a):
        with tenant_context(tenant_a.pk):
            assert _gucs(replica) == _NO_GUCS


class TestConfiguredAliases:
    """Every alias in ``RLS_TENANTS["DATABASES"]`` follows the context, wherever it was entered."""

    @pytest.fixture
    def with_replica(self, replica):
        databases = {**django_settings.RLS_TENANTS, "DATABASES": [DEFAULT_DB_ALIAS, replica]}
        with override_settings(RLS_TENANTS=databases):
            yield replica

    def test_replica_follows_a_tenant_context(self, sample_orders, tenant_a, with_replica):
        with tenant_context(tenant_a.pk):
            assert _gucs(with_replica) == _read_as(tenant_a)
            assert _products(with_replica) == _TENANT_A_PRODUCTS
        assert _gucs(with_replica) == _NO_GUCS
        assert _products(with_replica) == []

    def test_admin_context_on_one_alias_applies_on_every_alias(self, with_replica):
        with admin_context(using=with_replica):
            assert _gucs(DEFAULT_DB_ALIAS) == _ADMIN_READ
        assert _gucs(DEFAULT_DB_ALIAS) == _NO_GUCS

    def test_other_aliases_are_settled_when_the_block_exits(self, with_replica):
        """Nothing is left there for SQL the guard does not see (``callproc()``, say)."""
        with admin_context():
            assert _gucs(with_replica) == _ADMIN_READ

        assert session_gucs(with_replica) == _NO_GUCS

    def test_admin_context_on_an_alias_outside_the_list_applies_on_the_listed_ones(
        self,
        replica,
    ):
        """``using`` is where the GUCs are written first, not a limit on where they apply."""
        with admin_context(using=replica):
            assert _gucs(DEFAULT_DB_ALIAS) == _ADMIN_READ


class TestNormalFlowsAddNoStatement:
    """The library's own writes are recorded, so the guard has nothing to do after them."""

    @pytest.fixture(autouse=True)
    def _settled(self):
        """Run a statement first: whatever an earlier test left, the guard has seen it."""
        get_guc("rls.current_tenant")

    def test_tenant_context(self, tenant_a):
        with CaptureQueriesContext(connection) as queries, tenant_context(tenant_a.pk):
            list(Order.objects.all())

        # Snapshot (2), set (2), the query, restore (2), as without the guard.
        assert len(queries) == 7
        assert _guard_statements(queries) == []

    def test_nested_contexts(self, tenant_a, tenant_b):
        with CaptureQueriesContext(connection) as queries, tenant_context(tenant_a.pk):
            with tenant_context(tenant_b.pk), admin_context():
                list(Order.objects.all())
            list(Order.objects.all())

        assert _guard_statements(queries) == []

    def test_for_user_inside_a_context(self, tenant_a, tenant_b_user):
        with CaptureQueriesContext(connection) as queries, tenant_context(tenant_a.pk):
            Order.objects.for_user(tenant_b_user).count()
            list(Order.objects.all())

        assert _guard_statements(queries) == []

    def test_middleware_request(self, tenant_a_user):
        middleware = RLSTenantMiddleware(get_response=lambda request: None)
        request = _request_for(tenant_a_user)

        with CaptureQueriesContext(connection) as queries:
            middleware.process_request(request)
            list(Order.objects.all())
            middleware.process_response(request, None)

        assert _guard_statements(queries) == []

    def test_for_user(self, tenant_a_user):
        with CaptureQueriesContext(connection) as queries:
            Order.objects.for_user(tenant_a_user).count()

        assert _guard_statements(queries) == []


class TestFailures:
    def test_failed_apply_raises_before_the_statement(self, tenant_a, monkeypatch):
        """Fail closed: the statement never runs with the wrong GUCs."""
        reached_the_driver = []

        def remember_sql(execute, sql, params, many, context):
            reached_the_driver.append(sql)
            return execute(sql, params, many, context)

        def query_in_context():
            with _context_of(tenant_a):
                list(Order.objects.all())

        with monkeypatch.context() as patch:
            patch.setattr(
                affinity,
                "_SET_GUCS_SQL",
                "SELECT set_config(%s, %s, %s), no_such_function(%s, %s, %s)",
            )
            with (
                connection.execute_wrapper(remember_sql),  # inside the guard: sees what it passes
                pytest.raises(DatabaseError, match="no_such_function"),
                transaction.atomic(),
            ):
                query_in_context()

        assert any("no_such_function" in sql for sql in reached_the_driver)
        assert not any(Order._meta.db_table in sql for sql in reached_the_driver)
        # Recorded as unknown, so the next statement applies the context.
        with _context_of(tenant_a):
            assert _gucs() == _read_as(tenant_a)

    def test_failed_transaction_is_left_to_fail_on_its_own(self, tenant_a):
        """No guard statement in an aborted transaction: the caller sees PostgreSQL's error."""

        def select_one():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")

        def in_failed_transaction():
            with pytest.raises(DatabaseError), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
            with (
                _context_of(tenant_a),
                CaptureQueriesContext(connection) as queries,
                pytest.raises(DatabaseError, match="current transaction is aborted"),
            ):
                select_one()
            return [query["sql"] for query in queries]

        assert _in_atomic(in_failed_transaction, roll_back=True) == ["SELECT 1"]

    def test_failed_library_write_makes_the_values_unknown(self, tenant_a):
        """A ``writing()`` block that raises after a partial write leaves nothing trusted."""

        def write_partly():
            with affinity.writing(connection, ADMIN_GUCS):
                set_guc("rls.current_tenant", "999")
                raise _Failed

        with tenant_context(tenant_a.pk):
            with pytest.raises(_Failed):
                write_partly()

            assert _gucs() == _read_as(tenant_a)

    def test_skipped_restore_makes_the_values_unknown(self, tenant_a):
        """A restore that fails while an exception propagates is skipped, and not trusted."""

        def fail_the_is_admin_restore(execute, sql, params, many, context):
            if params == ["rls.is_admin", "false"]:  # set_guc() of the restore
                msg = "injected"
                raise OperationalError(msg)
            return execute(sql, params, many, context)

        with tenant_context(tenant_a.pk):
            with (
                pytest.raises(_Failed),
                connection.execute_wrapper(fail_the_is_admin_restore),
                admin_context(),
            ):
                raise _Failed

            assert session_gucs() == (str(tenant_a.pk), "true")  # the admin flag stayed
            assert _gucs() == _read_as(tenant_a)


class TestTracking:
    def test_session_the_guard_did_not_see_open_is_unknown(self):
        """No connection_created for it: whatever it holds is cleared outside a context."""
        set_guc("rls.current_tenant", "999")  # a value from no context
        _tracker().raw = None  # another session, as far as it knows

        assert _gucs() == _NO_GUCS

    def test_local_mode_records_what_a_block_writes(self, tenant_a):
        """Inside the test transaction the block writes ``SET LOCAL`` values, and restores them."""
        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local):
            with tenant_context(tenant_a.pk):
                assert _tracker().values == ContextGucs.for_tenant(tenant_a.pk)
                assert _tracker().wrote_local is True
            assert _tracker().values is None

    def test_alias_without_a_connection_is_ignored(self):
        affinity._record("no_such_alias", None)

        with pytest.raises(_Failed), affinity.writing("no_such_alias", None):
            raise _Failed

    def test_connection_without_a_tracker_runs_its_statements_unguarded(self, tenant_a):
        tracker = _tracker()
        delattr(connection, affinity._TRACKER_ATTR)
        try:
            with _context_of(tenant_a):
                assert _gucs() == _NO_GUCS
        finally:
            setattr(connection, affinity._TRACKER_ATTR, tracker)

    def test_install_keeps_the_record_of_a_session_it_already_tracks(self, tenant_a):
        """Another ``connection_created`` receiver ran a statement through the guard first."""
        with _context_of(tenant_a):
            _gucs()  # applied and recorded
            affinity.install(connection, values=None)

            assert _tracker().values == ContextGucs.for_tenant(tenant_a.pk)

    def test_install_of_a_pooled_session_starts_over(self, tenant_a):
        with _context_of(tenant_a):
            _gucs()
            affinity.install(connection, values=affinity.UNKNOWN)

            assert _tracker().values is affinity.UNKNOWN

    @pytest.mark.django_db(transaction=True)
    def test_write_gucs_locally_needs_a_transaction(self):
        with pytest.raises(RuntimeError, match="outside a transaction"):
            affinity.write_gucs(connection, ADMIN_GUCS, is_local=True)


class TestInstall:
    def test_guard_is_first_on_a_connection_that_never_had_it(self):
        """Django's ``execute_wrapper()`` removes the last wrapper on exit; the guard is first."""

        def passthrough(execute, sql, params, many, context):
            return execute(sql, params, many, context)

        conn = connections.create_connection(DEFAULT_DB_ALIAS)
        try:
            with conn.execute_wrapper(passthrough):
                conn.ensure_connection()  # connection_created inside the block
            assert conn.execute_wrappers == [affinity._guard]
        finally:
            conn.close()

    def test_other_backends_are_left_alone(self):
        other = SimpleNamespace(vendor="sqlite", execute_wrappers=[])

        affinity.install(other, values=None)

        assert other.execute_wrappers == []

    def test_is_installed_once(self):
        affinity.install(connection, values=None)
        affinity.install(connection, values=None)

        assert connection.execute_wrappers.count(affinity._guard) == 1

    @pytest.fixture
    def unguarded(self):
        """The test connection as if it had opened before the app's ``ready()``."""
        connection.ensure_connection()
        connection.execute_wrappers.remove(affinity._guard)
        delattr(connection, affinity._TRACKER_ATTR)
        yield
        if affinity._guard not in connection.execute_wrappers:
            affinity.install(connection, values=affinity.UNKNOWN)

    def test_connection_opened_before_the_handler_is_guarded_on_first_write(
        self,
        unguarded,
        tenant_a,
        tenant_b,
    ):
        """E.g. one another app opened in its ``ready()``, before this app connected the signal."""
        with tenant_context(tenant_a.pk):
            assert affinity._guard in connection.execute_wrappers
            with _context_of(tenant_b):
                assert _gucs() == _read_as(tenant_b)


class TestSafetyNet:
    def test_safety_net_stops_the_guard_applying_the_request(self, tenant_a_user):
        middleware = RLSTenantMiddleware(get_response=lambda request: None)
        middleware.process_request(_request_for(tenant_a_user))  # process_response never runs

        # close_old_connections would close the test's connection.
        request_finished.disconnect(close_old_connections)
        try:
            request_finished.send(sender=self.__class__)
        finally:
            request_finished.connect(close_old_connections)

        assert _get_context_gucs() is None
        assert _gucs() == _NO_GUCS


@pytest.mark.django_db(transaction=True)
class TestDjangoTransactionEnds:
    """What Django's commit and rollback really did, read from the transaction status."""

    def test_commit_of_a_failed_transaction_is_a_rollback(self, tenant_a, tenant_b):
        """A raw SQL error caught inside atomic(): Django commits, PostgreSQL rolls back."""

        def fail_and_carry_on():
            _gucs()  # tenant A applied in the transaction
            with pytest.raises(DatabaseError), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")

        with admin_context(), _context_of(tenant_a):  # the admin bypass is committed
            _in_atomic(fail_and_carry_on, roll_back=False)

            assert session_gucs() == _ADMIN_READ
            assert _gucs() == _read_as(tenant_a)

    def test_commit_that_ends_nothing_is_not_followed(self, tenant_a, tenant_b):
        """psycopg2 sends nothing for commit() in autocommit mode; a SQL BEGIN stays open."""
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            with connection.cursor() as cursor:
                cursor.execute("BEGIN")
                _gucs()  # tenant B applied in the transaction
                connection.commit()
                cursor.execute("ROLLBACK")

            assert _gucs() == _read_as(tenant_b)


class TestContextThatEndsFirst:
    """A block in a copy of a context that ends before the block does."""

    @pytest.mark.django_db(transaction=True)
    def test_block_in_a_thread_does_not_bring_back_an_ended_context(
        self,
        sample_orders,
        tenant_b,
        worker,
        enforce_rls_on_other_threads,
    ):
        entered, outer_ended = threading.Event(), threading.Event()

        def nested_block():
            with tenant_context(tenant_b.pk):
                entered.set()
                outer_ended.wait(timeout=10)

        with admin_context():
            job = worker.submit(contextvars.copy_context().run, nested_block)
            entered.wait(timeout=10)
        outer_ended.set()
        job.result(timeout=10)

        assert _hop(worker, lambda: (session_gucs(), _products()), contextvars.Context()) == (
            _NO_GUCS,
            [],
        )

    def test_block_in_a_task_does_not_bring_back_an_ended_context(
        self,
        sample_orders,
        tenant_b,
        enforce_rls,
    ):
        async def run():
            entered, outer_ended = asyncio.Event(), asyncio.Event()

            async def nested_block():
                async with async_tenant_context(tenant_b.pk):
                    entered.set()
                    await outer_ended.wait()

            async with async_admin_context():
                task = asyncio.ensure_future(nested_block())
                await entered.wait()
            outer_ended.set()
            await task
            return await sync_to_async(lambda: (session_gucs(), _products()))()

        assert async_to_sync(run)() == (_NO_GUCS, [])


async def _context_and_products():
    """What a coroutine sees: the active context's values, and the rows raw SQL returns."""
    return _get_context_gucs(), await sync_to_async(_products)()


class _AdminBlockOnAWorker:
    """An admin block that a ``thread_sensitive=False`` call runs, and its caller gives up on."""

    def __init__(self):
        self._release = threading.Event()
        self._done = threading.Event()

    def _run(self, give_up):
        try:
            with admin_context():
                give_up()
                self._release.wait(timeout=10)
        finally:
            connections.close_all()
            self._done.set()

    async def give_up_while_it_runs(self):
        """Await the call in this task, and cancel the await once the block has started.

        asgiref then copies the call's context, which holds the block's, back here.
        """
        with pytest.raises(TimeoutError):
            await self._await_until_the_block_runs()

    async def _await_until_the_block_runs(self):
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(None) as deadline:

            def give_up():
                loop.call_soon_threadsafe(lambda: deadline.reschedule(loop.time()))

            await sync_to_async(self._run, thread_sensitive=False)(give_up)

    def finish(self):
        """Let the block end, and wait until it has."""
        self._release.set()
        assert self._done.wait(timeout=10)


class TestCancelledHop:
    """asgiref copies a cancelled ``sync_to_async`` call's context into its caller."""

    def test_caller_keeps_its_own_context(self, sample_orders, tenant_a, enforce_rls):
        block = _AdminBlockOnAWorker()

        async def run():
            try:
                async with async_tenant_context(tenant_a.pk):
                    await block.give_up_while_it_runs()
                    return await _context_and_products()
            finally:
                await sync_to_async(block.finish, thread_sensitive=False)()

        assert async_to_sync(run)() == (ContextGucs.for_tenant(tenant_a.pk), _TENANT_A_PRODUCTS)

    @staticmethod
    def _set_and_return(block):
        _set_context_gucs(ContextGucs.for_tenant(9), block=block)  # never reset: as if cut short

    @pytest.mark.parametrize(
        ("caller_holds", "block", "copied_back"),
        [(True, True, False), (True, False, True), (False, True, True), (False, False, True)],
    )
    def test_a_blocks_holder_never_replaces_the_callers(self, caller_holds, block, copied_back):
        """Pins asgiref's rule: a value is copied back where it compares unequal (``!=``)."""

        async def run():
            if caller_holds:
                _hold_context_gucs()
            await sync_to_async(self._set_and_return)(block)
            return _get_context_gucs()

        values = contextvars.Context().run(async_to_sync(run))

        assert values == (ContextGucs.for_tenant(9) if copied_back else None)

    def test_anonymous_async_request_keeps_out_a_cancelled_hops_block(
        self,
        sample_orders,
        enforce_rls,
    ):
        """Served outside any context, the request holds one of no context."""
        block = _AdminBlockOnAWorker()

        async def view(request):
            await block.give_up_while_it_runs()
            return await _context_and_products()

        async def run():
            try:
                return await RLSTenantMiddleware(view)(_request_for(AnonymousUser()))
            finally:
                await sync_to_async(block.finish, thread_sensitive=False)()

        assert contextvars.Context().run(async_to_sync(run)) == (None, [])

    def test_coroutine_of_an_anonymous_wsgi_view_keeps_out_a_cancelled_hops_block(
        self,
        sample_orders,
        enforce_rls,
    ):
        """``async_to_sync`` hands the coroutine the WSGI request's holder."""
        block = _AdminBlockOnAWorker()

        async def give_up_then_read():
            await block.give_up_while_it_runs()
            return await _context_and_products()

        def view(request):
            try:
                return async_to_sync(give_up_then_read)()
            finally:
                block.finish()

        middleware = RLSTenantMiddleware(view)

        seen = contextvars.Context().run(middleware, _request_for(AnonymousUser()))

        assert seen == (None, [])


def _products_on_this_thread_then_close():
    """``_products()`` on the calling thread's connection, then close it (a worker's)."""
    try:
        return _products()
    finally:
        connections.close_all()


class TestAsyncToSyncInsideABlock:
    """``async_to_sync`` called in a block (a Celery task, say) runs in the block's context."""

    @pytest.mark.parametrize("block", ["tenant", "admin"])
    def test_coroutine_inherits_the_block(self, block, sample_orders, tenant_a, enforce_rls):
        if block == "tenant":
            context, values, products = (
                tenant_context(tenant_a.pk),
                ContextGucs.for_tenant(tenant_a.pk),
                _TENANT_A_PRODUCTS,
            )
        else:
            context, values, products = admin_context(), ADMIN_GUCS, _ALL_PRODUCTS

        async def count_orders():
            return await Order.objects.acount()

        with context:
            seen = async_to_sync(_context_and_products)()
            count = async_to_sync(count_orders)()

        assert seen == (values, products)
        assert count == len(products)

    @pytest.mark.django_db(transaction=True)
    def test_coroutines_worker_hop_gets_the_block(
        self,
        sample_orders,
        tenant_a,
        enforce_rls_on_other_threads,
    ):
        async def on_a_worker():
            return await sync_to_async(
                _products_on_this_thread_then_close,
                thread_sensitive=False,
            )()

        with tenant_context(tenant_a.pk):
            products = async_to_sync(on_a_worker)()

        assert products == _TENANT_A_PRODUCTS


class _HookPathMiddleware(RLSTenantMiddleware):
    """Overrides a sync hook, so ``MiddlewareMixin.__acall__`` serves its async requests."""

    def process_request(self, request):
        assert self._sync_hooks_overridden
        return super().process_request(request)


def _scope():
    """The tenant ``ContextVar`` values and the guard's: what a block's code runs with."""
    return get_current_tenant_id(), get_rls_context_active(), _get_context_gucs()


async def _view_products(request):
    return await sync_to_async(_products)()


async def _admin_rows():
    """An async generator that suspends inside an admin block."""
    async with async_admin_context():
        yield 1
        yield 2


def _admin_rows_sync():
    """A generator that suspends inside an admin block."""
    with admin_context():
        yield 1
        yield 2


_PATHS = [
    pytest.param(RLSTenantMiddleware, True, id="native-user"),
    pytest.param(RLSTenantMiddleware, False, id="native-anonymous"),
    pytest.param(_HookPathMiddleware, True, id="hooks-user"),
    pytest.param(_HookPathMiddleware, False, id="hooks-anonymous"),
]


class TestBlockLeftOpenByTheView:
    """A view that keeps a generator suspended inside a context block (#41).

    The block's context stays current in the request. It must not reach the
    error response Django builds after ``process_exception``, nor outlive the request.
    """

    @pytest.mark.parametrize(("middleware_class", "authenticated"), _PATHS)
    def test_async_error_response_runs_without_it(
        self,
        middleware_class,
        authenticated,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        async def view(request):
            rows = _admin_rows()
            await anext(rows)
            # What Django does when the view raises: process_exception in a hop.
            await sync_to_async(middleware.process_exception)(request, RuntimeError("boom"))
            seen = await _context_and_products()  # what the error response would see
            await rows.aclose()
            return seen

        middleware = middleware_class(view)
        user = tenant_a_user if authenticated else AnonymousUser()

        assert async_to_sync(middleware)(_request_for(user)) == (None, [])

    @pytest.mark.parametrize("authenticated", [True, False])
    def test_wsgi_error_response_runs_without_it(
        self,
        authenticated,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        def view(request):
            rows = _admin_rows_sync()
            next(rows)
            middleware.process_exception(request, RuntimeError("boom"))
            seen = _get_context_gucs(), _products()
            rows.close()
            return seen

        middleware = RLSTenantMiddleware(view)
        user = tenant_a_user if authenticated else AnonymousUser()

        assert middleware(_request_for(user)) == (None, [])

    def test_wsgi_local_mode_error_response_runs_without_it(
        self,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        """With ``USE_LOCAL_SET=True`` the middleware owns the request transaction.

        ``process_exception`` keeps the request's context for the error
        response there, but not the block's admin bypass: once the block is
        ended, the guard takes its ``SET LOCAL`` values off (fail-closed).
        """

        def view(request):
            rows = _admin_rows_sync()
            next(rows)
            middleware.process_exception(request, RuntimeError("boom"))
            seen = _get_context_gucs(), _products()
            rows.close()
            return seen

        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local):
            middleware = RLSTenantMiddleware(view)
            seen = middleware(_request_for(tenant_a_user))

        assert seen == (None, [])

    @pytest.mark.parametrize("left_open", [True, False])
    @pytest.mark.parametrize(("middleware_class", "authenticated"), _PATHS)
    def test_500_response_runs_without_it(
        self,
        middleware_class,
        authenticated,
        left_open,
        tenant_a_user,
    ):
        """Errors ``process_exception`` does not see (a view that returns ``None``, say)."""

        async def view(request):
            rows = _admin_rows()
            if left_open:
                await anext(rows)

            def build_500_response():
                # What Django does first in the hop that builds the 500 response.
                got_request_exception.send(sender=None, request=request)
                return _get_context_gucs()

            seen = await sync_to_async(build_500_response, thread_sensitive=False)()
            await rows.aclose()
            return seen

        user = tenant_a_user if authenticated else AnonymousUser()
        own = ContextGucs.for_tenant(tenant_a_user.rls_tenant_id) if authenticated else None

        seen = async_to_sync(middleware_class(view))(_request_for(user))

        # The request's own context is inherited (#41); a block left open is not.
        assert seen == (None if left_open else own)

    def test_error_signal_without_a_request_ends_nothing(self, tenant_a):
        with tenant_context(tenant_a.pk):
            got_request_exception.send(sender=None)
            assert _get_context_gucs() == ContextGucs.for_tenant(tenant_a.pk)

    def test_process_exception_ends_it_even_if_the_cleanup_fails(
        self,
        tenant_a_user,
        monkeypatch,
    ):
        """Native path: the request's token reset fails in the hop, so nothing else ends it."""

        def clear_fails(conf):
            msg = "connection lost"
            raise OperationalError(msg)

        async def view(request):
            rows = _admin_rows()
            await anext(rows)
            monkeypatch.setattr(middleware_module, "_clear_request_gucs", clear_fails)
            with pytest.raises(OperationalError, match="connection lost"):
                await sync_to_async(middleware.process_exception)(request, RuntimeError("boom"))
            monkeypatch.undo()
            seen = _get_context_gucs()
            await rows.aclose()
            return seen

        middleware = RLSTenantMiddleware(view)

        assert async_to_sync(middleware)(_request_for(tenant_a_user)) is None

    @pytest.mark.parametrize(("middleware_class", "authenticated"), _PATHS)
    def test_async_request_ends_it(self, middleware_class, authenticated, tenant_a_user):
        left_open = []

        async def view(request):
            rows = _admin_rows()
            await anext(rows)
            left_open.append((rows, contextvars.copy_context()))
            return "response"

        user = tenant_a_user if authenticated else AnonymousUser()

        async def run():
            await middleware_class(view)(_request_for(user))
            rows, escaped = left_open[0]
            try:
                return escaped.run(_get_context_gucs)
            finally:
                await rows.aclose()

        assert async_to_sync(run)() is None

    @pytest.mark.parametrize("authenticated", [True, False])
    def test_wsgi_request_ends_it(self, authenticated, tenant_a_user):
        left_open = []

        def view(request):
            rows = _admin_rows_sync()
            next(rows)
            left_open.append((rows, contextvars.copy_context()))
            return "response"

        user = tenant_a_user if authenticated else AnonymousUser()
        RLSTenantMiddleware(view)(_request_for(user))
        rows, escaped = left_open[0]

        assert escaped.run(_get_context_gucs) is None
        rows.close()


class TestRequestHolderAfterProcessException:
    """``process_exception`` runs in a hop of its own; the request keeps a holder after it."""

    @pytest.mark.parametrize(("middleware_class", "authenticated"), _PATHS)
    def test_cancelled_call_cannot_hand_its_block_to_the_request(
        self,
        middleware_class,
        authenticated,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        block = _AdminBlockOnAWorker()

        async def view(request):
            try:
                await sync_to_async(middleware.process_exception)(request, RuntimeError("boom"))
                # An error handler inside admin_context(), given up on (the client went away).
                await block.give_up_while_it_runs()
                return await _context_and_products()
            finally:
                await sync_to_async(block.finish, thread_sensitive=False)()

        middleware = middleware_class(view)
        user = tenant_a_user if authenticated else AnonymousUser()

        assert async_to_sync(middleware)(_request_for(user)) == (None, [])


class TestRequestInsideABlock:
    """A request served inside a context block (a test that wraps the client, say)."""

    @pytest.mark.parametrize("middleware_class", [RLSTenantMiddleware, _HookPathMiddleware])
    def test_async_request_runs_as_its_user_and_gives_the_block_back(
        self,
        middleware_class,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        async def run():
            async with async_admin_context():
                products = await middleware_class(_view_products)(_request_for(tenant_a_user))
                return products, await _context_and_products()

        assert async_to_sync(run)() == (_TENANT_A_PRODUCTS, (ADMIN_GUCS, _ALL_PRODUCTS))

    @pytest.mark.parametrize("middleware_class", [RLSTenantMiddleware, _HookPathMiddleware])
    def test_async_request_that_raises_gives_the_block_back(
        self,
        middleware_class,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        async def failing_view(request):
            raise _Failed

        async def run():
            async with async_admin_context():
                with pytest.raises(_Failed):
                    await middleware_class(failing_view)(_request_for(tenant_a_user))
                return await _context_and_products()

        assert async_to_sync(run)() == (ADMIN_GUCS, _ALL_PRODUCTS)

    @pytest.mark.parametrize("view_fails", [False, True])
    @pytest.mark.parametrize("middleware_class", [RLSTenantMiddleware, _HookPathMiddleware])
    def test_anonymous_async_request_keeps_the_block(
        self,
        middleware_class,
        view_fails,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        async def view(request):
            if view_fails:
                # What Django does when the view raises: process_exception in a hop.
                await sync_to_async(middleware.process_exception)(request, RuntimeError("boom"))
            return await sync_to_async(_products)()

        middleware = middleware_class(view)

        async def run():
            async with async_tenant_context(tenant_a.pk):
                products = await middleware(_request_for(AnonymousUser()))
                return products, _scope(), await sync_to_async(_products)()

        assert async_to_sync(run)() == (
            _TENANT_A_PRODUCTS,
            (tenant_a.pk, True, ContextGucs.for_tenant(tenant_a.pk)),
            _TENANT_A_PRODUCTS,
        )

    def test_cancelled_hook_path_request_gives_the_block_back(
        self,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        """The cleanup Django's hooks skip runs before the request lets go of its holder."""

        async def run():
            started = asyncio.Event()

            async def view(request):
                started.set()
                await asyncio.Event().wait()

            async with async_admin_context():
                request = _HookPathMiddleware(view)(_request_for(tenant_a_user))
                task = asyncio.ensure_future(request)
                await asyncio.wait_for(started.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                return await _context_and_products()

        assert async_to_sync(run)() == (ADMIN_GUCS, _ALL_PRODUCTS)

    def test_request_cancelled_in_process_request_gives_the_block_back(
        self,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        """Cancelled before the hook's hop hands its values back: they never become current."""
        entered, release = threading.Event(), threading.Event()

        class SlowHook(RLSTenantMiddleware):
            def process_request(self, request):
                entered.set()
                release.wait(timeout=10)
                return super().process_request(request)

        async def run():
            async with async_admin_context():
                task = asyncio.ensure_future(SlowHook(_view_products)(_request_for(tenant_a_user)))
                assert await sync_to_async(entered.wait, thread_sensitive=False)(timeout=10)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                return await _context_and_products()

        assert async_to_sync(run)() == (ADMIN_GUCS, _ALL_PRODUCTS)

    def test_request_object_served_twice_does_not_end_the_block(
        self,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        """The native path takes its token off the request once the request is over."""

        async def view(request):
            if not request.user.is_authenticated:
                # What Django does when the view raises: process_exception in a hop.
                await sync_to_async(middleware.process_exception)(request, RuntimeError("boom"))
            return "response"

        middleware = RLSTenantMiddleware(view)
        request = _request_for(tenant_a_user)

        async def run():
            async with async_admin_context():
                await middleware(request)
                request.user = AnonymousUser()
                await middleware(request)
                return await _context_and_products()

        assert async_to_sync(run)() == (ADMIN_GUCS, _ALL_PRODUCTS)

    def test_inner_middlewares_block_does_not_end_the_block(
        self,
        sample_orders,
        tenant_a,
        tenant_b,
        tenant_a_user,
        enforce_rls,
    ):
        """``process_exception`` runs inside a block an inner middleware opened for the view.

        The request's context is reset below that block, which then exits out of order.
        """

        def view(request):
            raise _Failed

        def scoping_middleware(request):
            with tenant_context(tenant_b.pk):
                try:
                    return view(request)
                except _Failed as exc:
                    # What Django does inside the chain when the view raises.
                    middleware.process_exception(request, exc)
                    return "error response"

        middleware = RLSTenantMiddleware(scoping_middleware)

        with tenant_context(tenant_a.pk):
            middleware(_request_for(tenant_a_user))
            after = _get_context_gucs(), _products()

        assert after == (ContextGucs.for_tenant(tenant_a.pk), _TENANT_A_PRODUCTS)

    @pytest.mark.parametrize("view_fails", [False, True])
    def test_anonymous_request_keeps_the_block(
        self,
        view_fails,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        """WSGI: the request sets no context, so its cleanup has none to end."""

        def view(request):
            if view_fails:
                middleware.process_exception(request, RuntimeError("boom"))
            return _products()

        middleware = RLSTenantMiddleware(view)

        with tenant_context(tenant_a.pk):
            products = middleware(_request_for(AnonymousUser()))
            after = _scope(), _products()

        assert (products, after) == (
            _TENANT_A_PRODUCTS,
            ((tenant_a.pk, True, ContextGucs.for_tenant(tenant_a.pk)), _TENANT_A_PRODUCTS),
        )


class TestSettleWithABrokenAlias:
    @pytest.fixture
    def with_replica(self, replica):
        databases = {**django_settings.RLS_TENANTS, "DATABASES": [DEFAULT_DB_ALIAS, replica]}
        with override_settings(RLS_TENANTS=databases):
            yield replica

    def test_block_exit_is_not_failed_by_another_alias(self, with_replica, tenant_a, monkeypatch):
        with tenant_context(tenant_a.pk):
            _gucs(with_replica)  # tenant A applied on the replica
            self._break(with_replica, monkeypatch)

        assert _tracker(connections[with_replica]).values is affinity.UNKNOWN

    def test_block_error_is_not_replaced(self, with_replica, tenant_a, monkeypatch):
        def raise_in_the_block():
            with tenant_context(tenant_a.pk):
                _gucs(with_replica)
                self._break(with_replica, monkeypatch)
                raise _Failed

        with pytest.raises(_Failed):
            raise_in_the_block()

    @staticmethod
    def _break(alias, monkeypatch):
        apply_if_stale = affinity._apply_if_stale

        def fail_on_the_alias(conn, tracker):
            if conn.alias == alias:
                msg = "server closed the connection unexpectedly"
                raise OperationalError(msg)
            return apply_if_stale(conn, tracker)

        monkeypatch.setattr(affinity, "_apply_if_stale", fail_on_the_alias)


class TestSavepointNames:
    def test_anything_after_the_name_makes_it_unclear(self, tenant_a, tenant_b):
        """``ROLLBACK TO s1 -- s2`` goes back to s1; the guard must not take s2's values."""

        def roll_back_with_a_trailing_comment():
            with connection.cursor() as cursor:
                cursor.execute("SAVEPOINT s1")  # tenant A
                with _context_of(tenant_b):
                    _gucs()  # tenant B applied
                    cursor.execute("SAVEPOINT s2")  # tenant B
                    cursor.execute("ROLLBACK TO s1 -- s2")
                    return session_gucs(), _gucs()

        with tenant_context(tenant_a.pk):
            session, gucs = _in_atomic(roll_back_with_a_trailing_comment, roll_back=True)

        assert (session, gucs) == (_read_as(tenant_a), _read_as(tenant_b))

    @pytest.mark.parametrize(
        ("sql", "name"),
        [
            ("SAVEPOINT a", "a"),
            ('RELEASE SAVEPOINT "A"', "A"),
            ("RELEASE b;", "b"),
            ("ROLLBACK TRANSACTION TO SAVEPOINT c", "c"),
            ("ROLLBACK TO d -- e", None),
            ('SAVEPOINT "two words"', None),
        ],
    )
    def test_savepoint_name(self, sql, name):
        statement = affinity._parse(sql)
        after = {"SAVEPOINT": "SAVEPOINT", "RELEASE": "RELEASE", "ROLLBACK": "TO"}

        assert affinity._savepoint_name(statement.words, after=after[statement.command]) == name


class TestContextEnd:
    def test_clear_ends_the_context_for_every_copy(self):
        """The middleware's fallback and the safety net end copies too (fail-closed)."""

        def start_then_clear():
            _set_context_gucs(ContextGucs.for_tenant(1))
            escaped = contextvars.copy_context()
            _clear_context_gucs()
            return escaped.run(_get_context_gucs), _get_context_gucs()

        assert contextvars.copy_context().run(start_then_clear) == (None, None)

    def test_reset_ends_an_inner_context_left_current(self):
        """An inner block left without its exit (a generator never closed) ends with the outer."""

        def outer_then_inner():
            outer = _set_context_gucs(ContextGucs.for_tenant(1))
            _set_context_gucs(ContextGucs.for_tenant(2))  # never reset
            escaped = contextvars.copy_context()
            _reset_context_gucs(outer)
            return escaped.run(_get_context_gucs), _get_context_gucs()

        assert contextvars.copy_context().run(outer_then_inner) == (None, None)


class TestInstallWrapsOnce:
    def test_commit_and_rollback_are_wrapped_once_however_often_install_runs(self):
        """Django's pool sends connection_created on every checkout: wrappers must not nest."""
        commit, rollback = connection._commit, connection._rollback

        affinity.install(connection, values=None)
        affinity.install(connection, values=affinity.UNKNOWN)

        assert (connection._commit, connection._rollback) == (commit, rollback)


class TestMoreSessionResets:
    @pytest.mark.django_db(transaction=True)
    def test_discard_all_makes_the_guard_apply_the_context_again(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute("DISCARD ALL")  # not allowed inside a transaction
            assert session_gucs() == _NO_GUCS
            assert _gucs() == _read_as(tenant_a)

    def test_reset_of_the_admin_guc_makes_the_guard_apply_the_context_again(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute("RESET rls.is_admin")
            assert session_gucs() == (str(tenant_a.pk), None)
            assert _gucs() == _read_as(tenant_a)


@pytest.mark.django_db(transaction=True)
class TestDriverCommits:
    """Commits on the driver connection, which the guard does not see."""

    def test_rollback_after_a_driver_commit_goes_back_to_what_it_committed(
        self,
        tenant_a,
        tenant_b,
    ):
        with tenant_context(tenant_a.pk):  # tenant A, committed in autocommit
            with _context_of(tenant_b):
                connection.set_autocommit(False)
                try:
                    _gucs()  # tenant B applied in the first transaction
                    connection.connection.commit()  # tenant B stays
                    _gucs()  # the guard sees the end, applies tenant B in the second one
                finally:
                    connection.rollback()  # Django's: tenant B (committed) is still there
                    connection.set_autocommit(True)
                assert session_gucs() == _read_as(tenant_b)

            assert _gucs() == _read_as(tenant_a)

    def test_driver_commit_is_not_taken_for_a_rollback(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk):
            with _context_of(tenant_b):
                connection.set_autocommit(False)
                try:
                    _gucs()  # tenant B applied in the transaction
                    connection.connection.commit()
                finally:
                    connection.connection.rollback()
                    connection.set_autocommit(True)
                assert session_gucs() == _read_as(tenant_b)

            assert _gucs() == _read_as(tenant_a)

    def test_driver_commit_without_a_change_needs_no_statement(self, tenant_a):
        with tenant_context(tenant_a.pk):
            connection.set_autocommit(False)
            try:
                _gucs()  # the session already holds tenant A
                connection.connection.commit()
                with CaptureQueriesContext(connection) as queries:
                    _gucs()
            finally:
                connection.connection.rollback()
                connection.set_autocommit(True)

        assert _guard_statements(queries) == []


class TestBlockExitedInAnotherContext:
    """A generator suspended inside a block, closed in another ``Context`` (GC, a finalizer)."""

    def test_generator_closed_in_another_context(
        self,
        sample_orders,
        tenant_a,
        tenant_b,
        enforce_rls,
    ):
        def stream():
            with admin_context():
                yield

        context_b = contextvars.Context()
        outer = tenant_context(tenant_b.pk)

        def start_in_b():
            outer.__enter__()
            generator = stream()
            next(generator)  # snapshot: tenant B's values
            return generator

        def close_in_a(generator):
            with tenant_context(tenant_a.pk):
                with pytest.raises(ValueError, match="different Context"):
                    generator.close()
                return _get_context_gucs(), _products()

        generator = context_b.run(start_in_b)
        try:
            seen = contextvars.Context().run(close_in_a, generator)
        finally:
            context_b.run(outer.__exit__, None, None, None)

        assert seen == (ContextGucs.for_tenant(tenant_a.pk), _TENANT_A_PRODUCTS)

    def test_generator_closed_on_another_thread_writes_nothing_there(self, tenant_b, worker):
        """That thread's session is not the one the block's snapshot describes."""

        def stream():
            with admin_context():
                yield

        context_b = contextvars.Context()
        outer = tenant_context(tenant_b.pk)

        def start_in_b():
            outer.__enter__()
            generator = stream()
            next(generator)  # snapshot: tenant B's values
            return generator

        def close_here(generator):
            with pytest.raises(ValueError, match="different Context"):
                generator.close()
            return session_gucs()

        generator = context_b.run(start_in_b)
        try:
            seen = _hop(worker, lambda: close_here(generator), contextvars.Context())
        finally:
            context_b.run(outer.__exit__, None, None, None)

        assert seen == _NO_GUCS

    def test_local_mode_restores_on_the_session_it_entered_on(
        self,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        """With ``USE_LOCAL_SET=True`` the restore puts the ``SET LOCAL`` values back too."""

        async def stream():
            async with async_admin_context():
                yield

        async def close_in_another_task():
            generator = stream()
            await anext(generator)
            with pytest.raises(ValueError, match="different Context"):
                await asyncio.create_task(generator.aclose())

        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with (
            override_settings(RLS_TENANTS=local),
            transaction.atomic(),
            tenant_context(tenant_a.pk),
        ):
            async_to_sync(close_in_another_task)()
            seen = session_gucs(), _products()

        assert seen == (_read_as(tenant_a), _TENANT_A_PRODUCTS)

    def test_local_mode_restores_no_ended_context(
        self,
        sample_orders,
        tenant_a,
        tenant_b,
        enforce_rls,
    ):
        """Closed after its enclosing block ended, the block restores no values of that block."""

        def stream():
            with admin_context():
                yield

        def start_inside_b():
            with tenant_context(tenant_b.pk):
                generator = stream()
                next(generator)  # snapshot: tenant B's values
            return generator

        def close_inside_a(generator):
            with tenant_context(tenant_a.pk):
                with pytest.raises(ValueError, match="different Context"):
                    contextvars.copy_context().run(generator.close)
                return session_gucs(), _products()

        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local), transaction.atomic():
            generator = contextvars.Context().run(start_inside_b)
            seen = contextvars.Context().run(close_inside_a, generator)

        # The restore cleared the session; the guard applied A again for the query.
        assert seen == (_NO_GUCS, _TENANT_A_PRODUCTS)

    def test_async_generator_closed_in_another_task(
        self,
        sample_orders,
        tenant_a,
        tenant_b,
        enforce_rls,
    ):
        async def stream():
            async with async_admin_context():
                yield

        async def run():
            started, done = asyncio.Event(), asyncio.Event()
            generators = []

            async def in_b():
                async with async_tenant_context(tenant_b.pk):
                    generator = stream()
                    await anext(generator)  # snapshot: tenant B's values
                    generators.append(generator)
                    started.set()
                    await done.wait()

            task = asyncio.create_task(in_b(), context=contextvars.Context())
            await started.wait()
            try:
                async with async_tenant_context(tenant_a.pk):
                    with pytest.raises(ValueError, match="different Context"):
                        await generators[0].aclose()
                    return await _context_and_products()
            finally:
                done.set()
                await task

        assert async_to_sync(run)() == (ContextGucs.for_tenant(tenant_a.pk), _TENANT_A_PRODUCTS)


@pytest.mark.django_db(transaction=True)
class TestCommitThatRaises:
    """A commit that raises: a failed one rolled back, an interrupted one may have committed."""

    @staticmethod
    def _commit_then_raise():
        """Commit on the driver, then fail, as an interruption after the server committed."""
        connection.connection.commit()
        raise _Failed

    def _values_after_the_commit(self):
        commit = affinity._transaction_end(connection, self._commit_then_raise, committed=True)
        connection.set_autocommit(False)
        try:
            _gucs()  # starts the transaction, with the context's values
            with pytest.raises(_Failed):
                commit()
            return _tracker().values
        finally:
            connection.connection.rollback()
            connection.set_autocommit(True)

    def test_values_set_in_the_transaction_become_unknown(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            values = self._values_after_the_commit()  # tenant B, set in the transaction
            assert session_gucs() == _read_as(tenant_b)  # committed

        assert values is affinity.UNKNOWN

    def test_values_the_transaction_did_not_change_are_kept(self, tenant_a):
        with tenant_context(tenant_a.pk):
            values = self._values_after_the_commit()

        assert values == ContextGucs.for_tenant(tenant_a.pk)


class TestSavepointBookkeeping:
    def test_released_savepoints_leave_no_entry(self):
        """A long transaction of many nested atomic() blocks must not grow the stack."""

        def nested_blocks():
            for _ in range(3):
                with transaction.atomic():
                    pass
            return _tracker().savepoints

        assert _in_atomic(nested_blocks, roll_back=True) == []

    def test_duplicate_savepoint_names_roll_back_to_the_newest(self, tenant_a, tenant_b):
        """PostgreSQL rolls back to the most recent savepoint of that name."""

        def run():
            with connection.cursor() as cursor:
                cursor.execute("SAVEPOINT dup")  # holds tenant A
                with _context_of(tenant_b):
                    _gucs()  # tenant B applied
                    cursor.execute("SAVEPOINT dup")  # holds tenant B
                    with _context(ADMIN_GUCS):
                        _gucs()  # admin applied
                    cursor.execute("ROLLBACK TO dup")
                    return session_gucs(), _tracker().values

        with tenant_context(tenant_a.pk):
            session, tracked = _in_atomic(run, roll_back=True)

        assert session == _read_as(tenant_b)
        assert tracked == ContextGucs.for_tenant(tenant_b.pk)

    @pytest.mark.django_db(transaction=True)
    def test_unseen_savepoint_rollback_needs_no_statement_after_an_earlier_change(
        self,
        tenant_a,
        tenant_b,
    ):
        """Whether values changed is judged per transaction, not over the connection's life."""

        def roll_back_to_a_driver_savepoint():
            with connection.connection.cursor() as driver_cursor:
                driver_cursor.execute("SAVEPOINT driver_sp")
            with connection.cursor() as cursor:
                cursor.execute("ROLLBACK TO SAVEPOINT driver_sp")
            with CaptureQueriesContext(connection) as queries:
                _gucs()
            return _guard_statements(queries)

        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            _in_atomic(_gucs, roll_back=False)  # tenant B applied and committed: a change

            assert _in_atomic(roll_back_to_a_driver_savepoint, roll_back=True) == []


class TestCompoundAndUnreadableStatements:
    def test_compound_statement_that_resets_makes_the_values_unknown(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1; RESET ALL")

            assert session_gucs() == _NO_GUCS
            assert _gucs() == _read_as(tenant_a)

    def test_statement_that_is_not_text_is_reconciled_before_and_forgotten_after(
        self,
        tenant_a,
        tenant_b,
    ):
        with tenant_context(tenant_a.pk), _context_of(tenant_b):
            with connection.cursor() as cursor:
                cursor.execute(pg_sql.SQL("SELECT current_setting('rls.current_tenant', true)"))
                seen = cursor.fetchone()[0]
            with connection.cursor() as cursor:
                cursor.execute(pg_sql.SQL("RESET ALL"))

            assert seen == str(tenant_b.pk)
            assert _gucs() == _read_as(tenant_b)

    def test_parse_sees_a_semicolon_after_a_long_head(self):
        sql = "SELECT '" + "x" * 300 + "'; RESET ALL"

        assert affinity._parse(sql).compound is True

    @pytest.mark.parametrize("sql", ["-- a\n  -- b\n  ROLLBACK", "/* a */\n\t-- b\n\tROLLBACK"])
    def test_indented_comments_are_skipped(self, sql):
        assert affinity._parse(sql).command == "ROLLBACK"


@pytest.mark.django_db(transaction=True)
class TestAsyncEnterOnAClosedConnection:
    def test_inner_block_snapshots_the_enclosing_context(self, tenant_a):
        """The hop's connection is new: the enclosing context must be on it before the snapshot."""

        def close_the_hop_threads_connection():
            # Looked up in the hop: connections are per thread.
            connections[DEFAULT_DB_ALIAS].close()

        async def run():
            async with async_admin_context():
                await sync_to_async(close_the_hop_threads_connection)()
                async with async_tenant_context(tenant_a.pk):
                    pass
                return await sync_to_async(_gucs)()

        assert async_to_sync(run)() == _ADMIN_READ


class TestMoreFlowsWithoutStatements:
    def test_for_user_matching_the_context_leaves_the_connection_as_it_was(
        self,
        tenant_a,
        tenant_a_user,
    ):
        with tenant_context(tenant_a.pk):
            Order.objects.for_user(tenant_a_user).count()
            with CaptureQueriesContext(connection) as queries:
                list(Order.objects.all())

        assert _guard_statements(queries) == []


class TestOtherAliasesOnExit:
    @pytest.fixture
    def with_replica(self, replica):
        databases = {**django_settings.RLS_TENANTS, "DATABASES": [DEFAULT_DB_ALIAS, replica]}
        with override_settings(RLS_TENANTS=databases):
            yield replica

    def test_alias_closed_inside_the_block_is_not_reopened_on_exit(self, with_replica):
        with admin_context():
            _gucs(with_replica)  # applied there: the tracker holds the admin values
            connections[with_replica].close()

        assert connections[with_replica].connection is None

    def test_safety_net_clears_every_configured_alias(self, with_replica, tenant_a_user):
        middleware = RLSTenantMiddleware(get_response=lambda request: None)
        middleware.process_request(_request_for(tenant_a_user))  # process_response never runs

        request_finished.disconnect(close_old_connections)
        try:
            with CaptureQueriesContext(connection) as queries:
                request_finished.send(sender=self.__class__)
        finally:
            request_finished.connect(close_old_connections)

        assert session_gucs(with_replica) == _NO_GUCS
        assert session_gucs() == _NO_GUCS
        assert _guard_statements(queries) == []


class TestRecordOnAClosedConnection:
    @pytest.mark.django_db(transaction=True)
    def test_is_a_no_op_and_does_not_reopen(self):
        connection.close()

        affinity._record(DEFAULT_DB_ALIAS, None)

        assert connection.connection is None


class TestTransactionEndBranches:
    @pytest.mark.django_db(transaction=True)
    def test_rollback_of_a_closed_connection_is_passed_through(self):
        connection.close()

        connection._rollback()  # Django's own no-op on a closed connection

        assert connection.connection is None

    def test_status_change_that_is_no_end_makes_the_values_unknown(self):
        tracker = affinity._SessionGUCs()
        tracker.attach(connection.connection, ContextGucs.for_tenant(1))
        tracker.read_status = lambda: affinity._IN_ERROR

        affinity._follow_end(tracker, affinity._IDLE, committed=True)

        assert tracker.values is affinity.UNKNOWN

    def test_savepoint_whose_name_cannot_be_read_makes_the_values_unknown(self, tenant_a):
        def set_it():
            with connection.cursor() as cursor:
                cursor.execute('SAVEPOINT "two words"')
            return _tracker().values

        with tenant_context(tenant_a.pk):
            assert _in_atomic(set_it, roll_back=True) is affinity.UNKNOWN
