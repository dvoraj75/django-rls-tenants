"""Tests for django_rls_tenants.tenants.affinity (the GUC affinity guard, #41)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings as django_settings
from django.core.signals import request_finished
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
from django.db.transaction import TransactionManagementError
from django.http import HttpRequest
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.rls.context import rls_context
from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants import affinity
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
    _get_context_gucs,
    _reset_context_gucs,
    _set_context_gucs,
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

    def test_local_mode_leaves_a_hop_without_context(self, tenant_a, worker):
        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local), tenant_context(tenant_a.pk):
            assert _gucs() == _read_as(tenant_a)
            assert _hop(worker, _gucs) == _NO_GUCS

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

    def test_local_mode_records_unknown(self, tenant_a):
        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local), tenant_context(tenant_a.pk):
            assert _tracker().values is affinity.UNKNOWN

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
