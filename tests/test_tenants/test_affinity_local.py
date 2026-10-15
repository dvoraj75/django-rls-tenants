"""Tests for django_rls_tenants.tenants.affinity with ``USE_LOCAL_SET=True`` (#96).

The GUC affinity guard writes the active context's values transaction-locally:
in a transaction, before the first statement that needs them; around a
statement run in autocommit, in a transaction of its own. Either way they end
with the transaction, so a session never keeps them.

Most tests use ``transaction=True``: autocommit is where the guard opens
transactions, and pytest-django's test transaction would hide it.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings as django_settings
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    DataError,
    IntegrityError,
    connection,
    connections,
    transaction,
)
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.apps import _guard_open_connections
from django_rls_tenants.rls.context import rls_context
from django_rls_tenants.rls.guc import set_guc
from django_rls_tenants.tenants import affinity
from django_rls_tenants.tenants.context import (
    admin_context,
    async_tenant_context,
    tenant_context,
    with_rls_context,
)
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware, _mark_gucs_set
from django_rls_tenants.tenants.state import ADMIN_GUCS, ContextGucs
from tests.session_gucs import session_gucs
from tests.test_app.models import Order
from tests.test_tenants.test_affinity import (
    _ADMIN_READ,
    _ALL_PRODUCTS,
    _NO_GUCS,
    _TENANT_A_PRODUCTS,
    _context_of,
    _Failed,
    _guard_statements,
    _gucs,
    _hop,
    _in_atomic,
    _products,
    _read_as,
    _request_for,
    _tracker,
    worker,  # noqa: F401  -- fixture
)

_IDLE = 0
_IN_TRANSACTION = 2
_LOCAL = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}


@pytest.fixture(autouse=True)
def local_mode():
    """``USE_LOCAL_SET=True`` for every test here, fixtures included."""
    with override_settings(RLS_TENANTS=_LOCAL):
        yield


def _status(conn=connection):
    """The session's transaction status, read from the driver."""
    conn.ensure_connection()
    return affinity._status_reader(conn.connection)()


def _logged(queries):
    """The SQL of captured queries, upper-cased and stripped."""
    return [(query["sql"] or "").strip().upper() for query in queries]


def _assert_cleared_after_a_context(conn=connection):
    """After a context, the session's own values count as unknown; the next statement clears them.

    Code in the context may have written them for the session (``set_guc()``,
    ``rls_context()`` without ``is_local=True``); the library itself never does.
    """
    assert _tracker(conn).values is affinity.UNKNOWN
    with CaptureQueriesContext(conn) as queries:
        assert _gucs(conn.alias) == _NO_GUCS
    assert len(_guard_statements(queries)) == 1
    assert _tracker(conn).values is None


class TestTracker:
    """``_SessionGUCs`` knows that a transaction-local value ends with its transaction."""

    @staticmethod
    def _in_transaction(values=None):
        tracker = affinity._SessionGUCs()
        status = [_IDLE]
        tracker.read_status = lambda: status[0]
        tracker.values = tracker.base = values
        status[0] = _IN_TRANSACTION
        tracker.sync_transaction()
        return tracker, status

    @pytest.mark.parametrize("committed", [True, False, None])
    def test_local_values_end_with_the_transaction(self, committed):
        tracker, _ = self._in_transaction()
        tracker.set_values(ADMIN_GUCS, local=True)
        tracker.end_transaction(committed=committed)
        assert tracker.values is None

    def test_unseen_end_of_a_local_transaction_brings_back_the_session_values(self):
        tracker, status = self._in_transaction(ContextGucs.for_tenant(1))
        tracker.set_values(ADMIN_GUCS, local=True)
        status[0] = _IDLE
        assert tracker.sync_transaction() == _IDLE
        assert tracker.values == ContextGucs.for_tenant(1)

    @pytest.mark.parametrize(
        ("committed", "expected"),
        [(True, ADMIN_GUCS), (False, None), (None, affinity.UNKNOWN)],
    )
    def test_session_values_survive_a_commit_only(self, committed, expected):
        tracker, _ = self._in_transaction()
        tracker.set_values(ADMIN_GUCS)
        tracker.end_transaction(committed=committed)
        assert tracker.values == expected

    @pytest.mark.parametrize("session_first", [True, False])
    def test_both_kinds_leave_unknown_values_after_a_commit(self, session_first):
        """Which one a commit leaves depends on their order (PostgreSQL's GUC stack)."""
        tracker, _ = self._in_transaction()
        writes = [(ADMIN_GUCS, False), (ContextGucs.for_tenant(1), True)]
        for values, local in writes if session_first else writes[::-1]:
            tracker.set_values(values, local=local)
        tracker.end_transaction(committed=True)
        assert tracker.values is affinity.UNKNOWN

    def test_both_kinds_are_undone_by_a_rollback(self):
        tracker, _ = self._in_transaction()
        tracker.set_values(ADMIN_GUCS)
        tracker.set_values(ContextGucs.for_tenant(1), local=True)
        tracker.end_transaction(committed=False)
        assert tracker.values is None

    def test_forgetting_in_a_transaction_counts_as_a_session_change(self):
        """``RESET rls.current_tenant`` in a transaction changes the session's value."""
        tracker, _ = self._in_transaction()
        tracker.set_values(ADMIN_GUCS, local=True)
        tracker.forget()
        tracker.end_transaction(committed=True)
        assert tracker.values is affinity.UNKNOWN


@pytest.mark.parametrize(
    ("sql", "refused"),
    [
        ("VACUUM test_order", True),
        ("vacuum;", True),
        ("/* maintenance */ CLUSTER", True),
        ("REINDEX TABLE CONCURRENTLY test_order", True),
        ("DISCARD ALL", True),
        ("CREATE DATABASE other", True),
        ("drop database if exists other", True),
        ("ALTER SYSTEM SET work_mem = '64MB'", True),
        ("CREATE TABLESPACE t LOCATION '/tmp'", True),
        ("DROP SUBSCRIPTION s", True),
        ("CREATE INDEX CONCURRENTLY i ON test_order (product)", True),
        ("CREATE UNIQUE INDEX CONCURRENTLY i ON test_order (product)", True),
        ("DROP INDEX CONCURRENTLY IF EXISTS i", True),
        ("ALTER TABLE t DETACH PARTITION p CONCURRENTLY", True),
        ("CREATE INDEX i ON test_order (product)", False),
        ("CREATE TABLE concurrently_log (id int)", False),
        ("ALTER TABLE t ADD COLUMN concurrently int", False),
        ("ALTER TABLE t DETACH PARTITION p", False),
        ("ALTER TABLE t DETACH PARTITION p FINALIZE", False),
        ("CREATE TEMP TABLE t AS SELECT product FROM test_order /* DETACH CONCURRENTLY */", False),
        ("CREATE TABLE t AS SELECT 1 /* INDEX CONCURRENTLY */", False),
        ("REFRESH MATERIALIZED VIEW CONCURRENTLY v", False),
        ("VACUUM(ANALYZE) test_order", True),
        ("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", False),  # transaction control
        ("SET work_mem = '64MB'", False),
        ("SELECT 'VACUUM'", False),
        ("INSERT INTO t VALUES (1)", False),
        ("CALL refresh()", False),
    ],
)
def test_statements_refused_in_a_transaction(sql, refused):
    assert affinity._refused_in_a_transaction(affinity._parse(sql)) is refused


def test_an_unreadable_statement_is_not_refused():
    assert affinity._refused_in_a_transaction(None) is False


@pytest.mark.parametrize(
    ("sql", "control"),
    [
        ("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", True),
        ("set transaction snapshot '00000003-0000001B-1'", True),
        ("BEGIN", True),
        ("SET TRANSACTION READ ONLY; SELECT 1", False),
        ("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", False),
        ("SET work_mem = '64MB'", False),
    ],
)
def test_statements_that_control_the_transaction(sql, control):
    assert affinity._controls_the_transaction(affinity._parse(sql)) is control


@pytest.mark.django_db(transaction=True)
class TestAutocommit:
    """A statement run in autocommit inside a context gets a transaction of its own."""

    def test_statements_see_the_context_and_the_session_keeps_nothing(
        self,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        with tenant_context(tenant_a.pk):
            assert connection.get_autocommit() is True
            seen = _gucs(), _products(), session_gucs(), _status()

        assert seen == (_read_as(tenant_a), _TENANT_A_PRODUCTS, _NO_GUCS, _IDLE)
        _assert_cleared_after_a_context()

    def test_admin_context_sees_every_row(self, sample_orders, enforce_rls):
        with admin_context():
            seen = _gucs(), _products()
        assert seen == (_ADMIN_READ, _ALL_PRODUCTS)
        assert session_gucs() == _NO_GUCS

    def test_no_context_needs_no_transaction(self, sample_orders, enforce_rls):
        connection.ensure_connection()
        with CaptureQueriesContext(connection) as queries:
            assert _products() == []
        assert len(queries) == 1

    def test_transaction_control_is_logged_like_djangos_own(
        self,
        sample_orders,
        tenant_a,
        monkeypatch,
    ):
        """``BEGIN`` and ``COMMIT`` are listed in ``connection.queries``, not seen by wrappers."""
        connection.ensure_connection()
        seen_by_wrappers = []

        def record(execute, sql, params, many, context):
            seen_by_wrappers.append(sql.strip().upper())
            return execute(sql, params, many, context)

        with (
            tenant_context(tenant_a.pk),
            connection.execute_wrapper(record),
            CaptureQueriesContext(connection) as queries,
        ):
            Order.objects.count()
            Order.objects.count()

        logged = _logged(queries)
        assert len(logged) == 8
        assert (logged.count("BEGIN"), logged.count("COMMIT")) == (2, 2)
        assert len(_guard_statements(queries)) == 2
        assert not {"BEGIN", "COMMIT"} & set(seen_by_wrappers)
        assert len(seen_by_wrappers) == 4

    def test_a_write_commits_on_its_own(self, tenant_a, enforce_rls):
        def create_then_fail():
            Order.objects.create(product="Also kept", amount=Decimal(1), tenant=tenant_a)
            raise _Failed

        with tenant_context(tenant_a.pk):
            Order.objects.create(product="Kept", amount=Decimal(1), tenant=tenant_a)
            with pytest.raises(_Failed):
                create_then_fail()
        # A new session sees both: each insert committed when it ran.
        connection.close()
        with admin_context():
            products = sorted(Order.objects.values_list("product", flat=True))
        assert products == ["Also kept", "Kept"]

    def test_a_failing_statement_is_rolled_back_alone(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with pytest.raises(DataError), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
            assert _status() == _IDLE
            assert _gucs() == _read_as(tenant_a)
        assert session_gucs() == _NO_GUCS
        _assert_cleared_after_a_context()

    def test_a_failing_commit_fails_the_statement(self, tenant_a):
        """A deferred foreign key is checked at the ``COMMIT``, as in autocommit."""
        with admin_context(), pytest.raises(IntegrityError):
            Order.objects.create(product="orphan", amount=Decimal(1), tenant_id=2**31 - 1)
        assert _status() == _IDLE
        assert session_gucs() == _NO_GUCS
        with admin_context():
            assert not Order.objects.filter(product="orphan").exists()

    def test_a_failing_write_of_the_values_skips_the_statement(self, tenant_a, monkeypatch):
        real = affinity.write_gucs

        def fail_locally(conn, values, *, is_local=False):
            if is_local:
                raise DatabaseError("set_config failed")
            real(conn, values, is_local=is_local)

        monkeypatch.setattr(affinity, "write_gucs", fail_locally)
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            with pytest.raises(DatabaseError, match="set_config failed"):
                cursor.execute("SELECT set_config('rls.probe', 'ran', false)")
            assert _status() == _IDLE
        monkeypatch.undo()
        assert _gucs() == _NO_GUCS
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('rls.probe', true)")
            assert cursor.fetchone()[0] in {None, ""}

    def test_server_side_cursor_rows_are_computed_with_the_context(
        self,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        """Django's autocommit cursors are ``WITH HOLD``: computed at the ``COMMIT``."""
        with tenant_context(tenant_a.pk):
            cursor = connection.chunked_cursor()
            try:
                cursor.execute("SELECT product FROM test_order ORDER BY product")
                first = cursor.fetchmany(1)
                status = _status()
                rest = cursor.fetchall()
            finally:
                cursor.close()
        assert [row[0] for row in first + rest] == _TENANT_A_PRODUCTS
        assert status == _IDLE

    def test_iterator(self, sample_orders, tenant_a, enforce_rls):
        with tenant_context(tenant_a.pk):
            products = [order.product for order in Order.objects.iterator(chunk_size=1)]
        assert sorted(products) == _TENANT_A_PRODUCTS

    def test_executemany_runs_in_one_transaction_of_its_own(self, tenant_a, enforce_rls):
        """The ``INSERT`` passes the policy's ``WITH CHECK`` only with the admin values."""
        with admin_context(), connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO test_order (product, amount, tenant_id) VALUES (%s, 1, %s)",
                [["Many 1", tenant_a.pk], ["Many 2", tenant_a.pk]],
            )
        with tenant_context(tenant_a.pk):
            assert _products() == ["Many 1", "Many 2"]

    def test_statements_refused_in_a_transaction_run_as_they_are(self, sample_orders):
        """None of them evaluates an RLS policy, so they run without the context.

        A temporary table: PostgreSQL still refuses ``CONCURRENTLY`` in a
        transaction block, but builds the index without waiting for other
        sessions' transactions.
        """
        with admin_context(), connection.cursor() as cursor:
            cursor.execute("VACUUM test_order")
            cursor.execute("CREATE TEMPORARY TABLE probe_refused (v int)")
            cursor.execute("CREATE INDEX CONCURRENTLY probe_refused_v ON probe_refused (v)")
            cursor.execute("DROP INDEX CONCURRENTLY probe_refused_v")
            cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        assert _status() == _IDLE

    def test_a_compound_statement_leaves_unknown_values(self, tenant_a):
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute("SELECT 1; SELECT 2")
        assert _tracker().values is affinity.UNKNOWN
        assert _gucs() == _NO_GUCS

    def test_values_left_for_the_session_are_cleared_outside_a_context(self):
        """After a change from session GUCs to transaction-local ones, say."""
        affinity.write_gucs(connection, ADMIN_GUCS)
        assert session_gucs() == _ADMIN_READ
        assert _gucs() == _NO_GUCS
        assert session_gucs() == _NO_GUCS

    def test_unknown_values_are_cleared_outside_a_context(self):
        set_guc("rls.is_admin", "true")
        _tracker().forget()
        assert _gucs() == _NO_GUCS

    @pytest.mark.parametrize("write", ["set_guc", "rls_context"])
    @pytest.mark.parametrize("transaction_open", [False, True], ids=["autocommit", "atomic"])
    def test_a_session_write_inside_a_context_does_not_outlive_it(
        self,
        sample_orders,
        tenant_a,
        tenant_b,
        enforce_rls,
        write,
        transaction_open,
    ):
        """Code inside the context writes the tenant GUC for the session (``is_local=False``).

        In session mode the context's exit restores the GUCs; here the
        session's own values count as unknown once the context ends, and the
        next statement outside a context clears them.
        """

        def inside():
            with tenant_context(tenant_a.pk):
                if write == "set_guc":
                    set_guc("rls.current_tenant", str(tenant_b.pk))
                else:
                    with rls_context({"rls.current_tenant": str(tenant_b.pk)}):
                        pass

        if transaction_open:
            with transaction.atomic():
                inside()
        else:
            inside()
        assert session_gucs() != _NO_GUCS  # control: the write outlived the context
        assert _products() == []
        assert session_gucs() == _NO_GUCS

    def test_an_async_request_clears_a_session_write_of_its_view(
        self,
        sample_orders,
        tenant_a_user,
        tenant_b,
        enforce_rls,
    ):
        async def view(request):
            await sync_to_async(set_guc)("rls.current_tenant", str(tenant_b.pk))
            return "response"

        assert async_to_sync(RLSTenantMiddleware(view))(_request_for(tenant_a_user)) == "response"
        assert session_gucs() != _NO_GUCS  # control
        assert _products() == []
        assert session_gucs() == _NO_GUCS

    def test_a_block_writes_nothing_itself(self, tenant_a):
        connection.ensure_connection()
        with CaptureQueriesContext(connection) as queries:
            with tenant_context(tenant_a.pk):
                pass
            with admin_context():
                pass
        assert queries.captured_queries == []

    def test_with_rls_context_function_needs_no_transaction(
        self,
        sample_orders,
        tenant_a_user,
        enforce_rls,
    ):
        @with_rls_context
        def products(as_user):
            return _products()

        assert products(as_user=tenant_a_user) == _TENANT_A_PRODUCTS
        assert session_gucs() == _NO_GUCS

    def test_rollback_that_fails_closes_the_connection(self, tenant_a, monkeypatch):
        """Django thinks the connection is in autocommit: nothing may run in that transaction.

        The statement's own error propagates, not the rollback's.
        """
        real = affinity._send_transaction_control

        def rollback_fails(conn, command):
            if command == "ROLLBACK":
                raise DatabaseError("rollback failed")
            real(conn, command)

        monkeypatch.setattr(affinity, "_send_transaction_control", rollback_fails)
        with tenant_context(tenant_a.pk):
            with pytest.raises(DataError), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
            assert connection.connection is None
            monkeypatch.undo()
            assert _gucs() == _read_as(tenant_a)
        assert _status() == _IDLE
        assert session_gucs() == _NO_GUCS

    def test_connection_that_cannot_close_is_left_to_django(self, tenant_a, monkeypatch):
        """Closing after an unconfirmed rollback is best effort: the statement's error wins."""
        real = affinity._send_transaction_control

        def rollback_fails(conn, command):
            if command == "ROLLBACK":
                raise DatabaseError("rollback failed")
            real(conn, command)

        def close_fails():
            raise DatabaseError("close failed")

        monkeypatch.setattr(affinity, "_send_transaction_control", rollback_fails)
        monkeypatch.setattr(connection, "close", close_fails)
        with tenant_context(tenant_a.pk), pytest.raises(DataError), connection.cursor() as cursor:
            cursor.execute("SELECT 1 / 0")
        monkeypatch.undo()
        assert _tracker().values is affinity.UNKNOWN
        connection.close()

    def test_interruption_inside_the_statement_rolls_back(self, tenant_a):
        """A ``BaseException`` (a server's timeout, say) ends the guard's transaction too."""

        class Interrupted(BaseException):
            pass

        def interrupt(execute, sql, params, many, context):
            raise Interrupted

        with tenant_context(tenant_a.pk):
            with pytest.raises(Interrupted), connection.execute_wrapper(interrupt):
                _gucs()
            assert _status() == _IDLE
        assert session_gucs() == _NO_GUCS


@pytest.mark.django_db(transaction=True)
class TestInATransaction:
    """In a transaction the guard writes the values once, and they end with it."""

    def test_values_are_written_once_per_transaction(self, sample_orders, tenant_a, enforce_rls):
        connection.ensure_connection()
        with tenant_context(tenant_a.pk):
            with transaction.atomic(), CaptureQueriesContext(connection) as queries:
                first, second = _products(), _products()
                inside = session_gucs()
            after = session_gucs()
        assert (first, second) == (_TENANT_A_PRODUCTS, _TENANT_A_PRODUCTS)
        assert len(_guard_statements(queries)) == 1
        assert (inside, after) == (_read_as(tenant_a), _NO_GUCS)

    def test_values_end_with_a_rollback(self, tenant_a):
        with tenant_context(tenant_a.pk):
            assert _in_atomic(_gucs, roll_back=True) == _read_as(tenant_a)
        assert session_gucs() == _NO_GUCS
        _assert_cleared_after_a_context()

    def test_savepoint_rollback_brings_the_enclosing_values_back(self, tenant_a, tenant_b):
        seen = []

        def read_as_b_then_fail():
            with _context_of(tenant_b):
                seen.append(_gucs())
                raise _Failed

        with tenant_context(tenant_a.pk), transaction.atomic():
            assert _gucs() == _read_as(tenant_a)
            with pytest.raises(_Failed), transaction.atomic():
                read_as_b_then_fail()
            assert seen == [_read_as(tenant_b)]
            assert session_gucs() == _read_as(tenant_a)
            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == _read_as(tenant_a)
            assert _guard_statements(queries) == []

    def test_block_entered_in_a_transaction_restores_on_exit(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk), transaction.atomic():
            assert _gucs() == _read_as(tenant_a)
            with tenant_context(tenant_b.pk):
                assert session_gucs() == _read_as(tenant_b)
            assert session_gucs() == _read_as(tenant_a)

    def test_autocommit_off_without_atomic(self, tenant_a):
        """The driver's transaction is still open when the block exits: it is settled."""
        transaction.set_autocommit(False)
        try:
            with tenant_context(tenant_a.pk):
                assert _gucs() == _read_as(tenant_a)
            assert session_gucs() == _NO_GUCS
            transaction.commit()
            assert session_gucs() == _NO_GUCS
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
        _assert_cleared_after_a_context()

    def test_marking_a_session_whose_status_cannot_be_read(self, tenant_a, monkeypatch):
        """A broken connection at the end of a context: its values are unknown, nothing raises."""
        connection.ensure_connection()

        def broken():
            raise DatabaseError("connection broken")

        monkeypatch.setattr(_tracker(), "read_status", broken)
        affinity.suspect_sessions()
        monkeypatch.undo()
        assert _tracker().values is affinity.UNKNOWN

    def test_unknown_values_in_an_open_transaction_are_settled_on_exit(self, tenant_a):
        """A compound statement leaves the values unknown; the block's exit still takes them off.

        SQL that bypasses the guard (a driver cursor) runs right after the block.
        """
        transaction.set_autocommit(False)
        try:
            with tenant_context(tenant_a.pk), connection.cursor() as cursor:
                cursor.execute("SELECT 1; SELECT 2")
                assert session_gucs() == _read_as(tenant_a)
                assert _tracker().values is affinity.UNKNOWN
            assert session_gucs() == _NO_GUCS
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)

    @pytest.mark.parametrize("use_local_set", [True, False], ids=["set_local", "set_config"])
    def test_set_transaction_first_in_a_transaction(
        self,
        sample_orders,
        tenant_a,
        enforce_rls,
        use_local_set,
    ):
        """``SET TRANSACTION`` must come first: the guard writes the values after it."""
        mode = {**_LOCAL, "USE_LOCAL_SET": use_local_set}
        with (
            override_settings(RLS_TENANTS=mode),
            tenant_context(tenant_a.pk),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            cursor.execute("SHOW transaction_isolation")
            isolation = cursor.fetchone()[0]
            products = _products()
        assert (isolation, products) == ("serializable", _TENANT_A_PRODUCTS)

    def test_autocommit_off_clears_the_session_once_after_a_context(self, tenant_a, tenant_b):
        """Without autocommit statements, the clear is made for the session and sticks."""
        transaction.set_autocommit(False)
        try:
            with tenant_context(tenant_a.pk):
                set_guc("rls.current_tenant", str(tenant_b.pk))  # for the session, by hand
            transaction.commit()
            with CaptureQueriesContext(connection) as first:
                assert _gucs() == _NO_GUCS
            transaction.commit()
            with CaptureQueriesContext(connection) as second:
                assert _gucs() == _NO_GUCS
            transaction.commit()
            unguarded = session_gucs()
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
        assert len(_guard_statements(first)) == 1
        assert _guard_statements(second) == []
        assert unguarded == _NO_GUCS

    def test_transaction_opened_with_sql(self, tenant_a):
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute("BEGIN")
            assert _gucs() == _read_as(tenant_a)
            assert session_gucs() == _read_as(tenant_a)
            cursor.execute("COMMIT")
            assert session_gucs() == _NO_GUCS
        _assert_cleared_after_a_context()


@pytest.mark.django_db(transaction=True)
class TestOtherConnections:
    """Hops, sibling tasks and other aliases follow the context transaction-locally."""

    def test_connection_opened_mid_request_gets_no_session_values(self, tenant_a):
        """On the request's thread too: the guard applies the values to each statement."""
        _mark_gucs_set()
        with _context_of(tenant_a):
            connection.close()
            assert session_gucs() == _NO_GUCS  # opens the connection
            assert _gucs() == _read_as(tenant_a)
            assert session_gucs() == _NO_GUCS

    def test_connection_opened_before_the_app_was_ready_is_guarded(
        self,
        sample_orders,
        tenant_a,
        enforce_rls,
    ):
        """Opened before the ``connection_created`` handler existed (another app's ``ready()``)."""
        connection.ensure_connection()
        tracker = _tracker()
        delattr(connection, affinity._TRACKER_ATTR)
        connection.execute_wrappers.remove(affinity._guard)
        try:
            with tenant_context(tenant_a.pk):
                unguarded = _products()
            _guard_open_connections()
            with tenant_context(tenant_a.pk):
                guarded = _products()
        finally:
            if affinity._guard not in connection.execute_wrappers:
                connection.execute_wrappers.insert(0, affinity._guard)
                setattr(connection, affinity._TRACKER_ATTR, tracker)
        assert (unguarded, guarded) == ([], _TENANT_A_PRODUCTS)

    def test_hop_inherits_the_context_and_its_session_keeps_nothing(
        self,
        sample_orders,
        tenant_a,
        worker,  # noqa: F811  -- fixture
        enforce_rls_on_other_threads,
    ):
        with tenant_context(tenant_a.pk):
            inside = _hop(worker, lambda: (_gucs(), _products(), session_gucs()))
        after = _hop(worker, lambda: (_gucs(), session_gucs()))
        assert inside == (_read_as(tenant_a), _TENANT_A_PRODUCTS, _NO_GUCS)
        assert after == (_NO_GUCS, _NO_GUCS)

    def test_a_session_write_in_a_hop_does_not_outlive_the_context(
        self,
        sample_orders,
        tenant_a,
        worker,  # noqa: F811  -- fixture
        enforce_rls_on_other_threads,
    ):
        """The hop's connection belongs to another thread, which no block exit reaches."""

        def write_for_the_session():
            with rls_context({"rls.current_tenant": str(tenant_a.pk)}):
                pass
            return session_gucs()

        with tenant_context(tenant_a.pk):
            left = _hop(worker, write_for_the_session)
        later = _hop(worker, lambda: (_products(), session_gucs()))
        assert left != _NO_GUCS  # control: the write outlived the hop's statement
        assert later == ([], _NO_GUCS)

    def test_a_session_write_in_a_hops_transaction_does_not_outlive_it(
        self,
        sample_orders,
        tenant_a,
        worker,  # noqa: F811  -- fixture
        enforce_rls_on_other_threads,
    ):
        """The same inside the hop's own ``atomic()``: unknown once that transaction commits."""

        def write_in_a_transaction():
            with transaction.atomic():
                _gucs()  # the guard sets the context's values in the transaction
                set_guc("rls.current_tenant", str(tenant_a.pk))  # for the session
            return session_gucs()

        # Opened (and its role switched) outside the context, so the only
        # statements run in it under the context are the transaction's.
        assert _hop(worker, _gucs) == _NO_GUCS
        with tenant_context(tenant_a.pk):
            left = _hop(worker, write_in_a_transaction)
        later = _hop(worker, lambda: (_products(), session_gucs()))
        assert left != _NO_GUCS  # control: the write outlived the transaction
        assert later == ([], _NO_GUCS)

    def test_sibling_tasks_see_their_own_context(self, tenant_a, tenant_b):
        """Each statement runs in a transaction of its own with its task's values."""

        async def read_as(tenant):
            seen = []
            async with async_tenant_context(tenant.pk):
                for _ in range(3):
                    seen.append(await sync_to_async(_gucs)())
                    await asyncio.sleep(0)
            return seen

        async def run():
            return await asyncio.gather(read_as(tenant_a), read_as(tenant_b))

        seen_a, seen_b = async_to_sync(run)()
        assert seen_a == [_read_as(tenant_a)] * 3
        assert seen_b == [_read_as(tenant_b)] * 3
        assert session_gucs() == _NO_GUCS

    def test_configured_replica_follows(self, replica, tenant_a):
        databases = {**_LOCAL, "DATABASES": [DEFAULT_DB_ALIAS, replica]}
        with override_settings(RLS_TENANTS=databases), tenant_context(tenant_a.pk):
            seen = _gucs(replica), session_gucs(replica)
        assert seen == (_read_as(tenant_a), _NO_GUCS)
        assert session_gucs(connections[replica]) == _NO_GUCS

    def test_alias_outside_rls_databases_does_not_follow(self, replica, tenant_a):
        with tenant_context(tenant_a.pk):
            assert _gucs(replica) == _NO_GUCS
        with (
            pytest.raises(RuntimeError, match="outside a transaction"),
            tenant_context(tenant_a.pk, using=replica),
        ):
            pytest.fail("The block must not run")
