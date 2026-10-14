"""Tests for django_rls_tenants.tenants.affinity (the GUC affinity guard, #41)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings as django_settings
from django.core.signals import request_finished
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
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
    _get_context_gucs,
    _reset_context_gucs,
    _set_context_gucs,
    set_current_tenant_id,
    set_rls_context_active,
)
from tests.test_app.models import Order

pytestmark = pytest.mark.django_db

_ALL_PRODUCTS = ["Gadget B1", "Widget A1", "Widget A2"]
_TENANT_A_PRODUCTS = ["Widget A1", "Widget A2"]


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
    """The tenant and admin GUCs, read with a Django cursor, so after the guard."""
    return get_guc("rls.current_tenant", using=using), get_guc("rls.is_admin", using=using)


def _session_gucs(using=DEFAULT_DB_ALIAS):
    """The same, read with a driver cursor, which the guard never sees."""
    conn = connections[using]
    conn.ensure_connection()
    with conn.connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('rls.current_tenant', true), "
            "current_setting('rls.is_admin', true)"
        )
        return tuple(value or None for value in cursor.fetchone())


def _products(using=DEFAULT_DB_ALIAS):
    """The products the connection's RLS policy lets through: raw SQL, so no ORM filter."""
    conn = connections[using]
    with conn.cursor() as cursor:
        table = conn.ops.quote_name(Order._meta.db_table)
        cursor.execute(f"SELECT product FROM {table} ORDER BY product")
        return [row[0] for row in cursor.fetchall()]


class _Failed(Exception):  # noqa: N818  -- a marker, not an error the code under test raises
    """Raised inside a block to see how it handles an exception."""


def _tenant_gucs(tenant):
    """What ``_gucs()`` reads in the tenant's context: ``(current_tenant, is_admin)``."""
    return str(tenant.pk), "false"


def _context_of(tenant):
    """The tenant's context GUC values, in the guard's order: ``(is_admin, current_tenant)``."""
    return "false", str(tenant.pk)


def _tracker():
    """The guard's record of the test connection's session."""
    return getattr(connection, affinity._TRACKER_ATTR)


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

    @pytest.mark.parametrize("context", ["tenant", "admin"])
    def test_hop_inherits_the_context(
        self,
        context,
        sample_orders,
        tenant_a,
        worker,
        enforce_rls_on_other_threads,
    ):
        block = tenant_context(tenant_a.pk) if context == "tenant" else admin_context()
        with block:
            gucs, products = _hop(worker, lambda: (_gucs(), _products()))

        if context == "tenant":
            assert (gucs, products) == (_tenant_gucs(tenant_a), _TENANT_A_PRODUCTS)
        else:
            assert (gucs, products) == ((None, "true"), _ALL_PRODUCTS)

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
            lambda: (_session_gucs(), _gucs(), _products()),
            contextvars.Context(),
        )

        assert session == _tenant_gucs(tenant_a)  # still on the session ...
        assert (gucs, products) == ((None, None), [])  # ... until the next statement

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

    def test_new_session_on_the_same_connection_object_gets_the_context(self, tenant_a):
        """A reconnect replaces the session; the guard tracks the new one from scratch."""
        with tenant_context(tenant_a.pk):
            connection.close()
            connection.ensure_connection()
            assert _session_gucs() == (None, None)
            assert _gucs() == _tenant_gucs(tenant_a)


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
        assert _gucs() == (None, None)
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
        assert _gucs() == (None, None)


class TestSavepointRollback:
    """``ROLLBACK TO SAVEPOINT`` undoes the ``set_config()`` calls made after the savepoint."""

    def test_guard_applies_the_context_again(self, tenant_a, tenant_b):
        tokens = []

        def switch_to_tenant_b():
            # Another task's context on the shared connection, as in TestSiblingTasks.
            tokens.append(_set_context_gucs(_context_of(tenant_b)))
            return _gucs()  # applied in the savepoint ...

        with tenant_context(tenant_a.pk):
            try:
                assert _in_atomic(switch_to_tenant_b, roll_back=True) == _tenant_gucs(tenant_b)

                assert _session_gucs() == _tenant_gucs(tenant_a)  # ... and undone with it
                assert _gucs() == _tenant_gucs(tenant_b)
            finally:
                _reset_context_gucs(tokens.pop())

    def test_rollback_to_a_savepoint_restores_the_values_it_saw(self, tenant_a, tenant_b):
        """Nested savepoints: the inner rollback brings back exactly what the outer one set."""
        tokens = []

        def outer():
            tokens.append(_set_context_gucs(_context_of(tenant_b)))
            _gucs()  # tenant B applied in the outer savepoint

            def inner():
                tokens.append(_set_context_gucs(("true", "")))
                _gucs()  # admin applied in the inner savepoint
                _reset_context_gucs(tokens.pop())

            _in_atomic(inner, roll_back=True)
            with CaptureQueriesContext(connection) as queries:
                gucs = _gucs()
            return gucs, len(queries), _session_gucs()

        with tenant_context(tenant_a.pk):
            try:
                gucs, statements, session = _in_atomic(outer, roll_back=True)
            finally:
                _reset_context_gucs(tokens.pop())

        assert gucs == session == _tenant_gucs(tenant_b)
        assert statements == 2  # the two reads: nothing to apply

    def test_savepoint_statements_are_not_reconciled(self, tenant_a):
        """The context is applied before the first data statement, inside the savepoint."""
        token = _set_context_gucs(_context_of(tenant_a))
        try:
            with CaptureQueriesContext(connection) as queries, transaction.atomic():
                pass
        finally:
            _reset_context_gucs(token)

        assert [query["sql"].split()[0] for query in queries] == ["SAVEPOINT", "RELEASE"]

    def test_rollback_without_a_change_needs_no_statement(self, tenant_a):
        with tenant_context(tenant_a.pk):
            _in_atomic(lambda: list(Order.objects.all()), roll_back=True)
            with CaptureQueriesContext(connection) as queries:
                assert get_guc("rls.current_tenant") == str(tenant_a.pk)
        assert len(queries) == 1


@pytest.mark.django_db(transaction=True)
class TestTransactionEnd:
    """A transaction keeps or undoes the values set in it; the guard cannot tell which."""

    @pytest.mark.parametrize("roll_back", [False, True], ids=["commit", "rollback"])
    def test_guard_applies_the_context_again(self, roll_back, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk):  # in autocommit: tenant A is committed
            token = _set_context_gucs(_context_of(tenant_b))
            try:
                assert _in_atomic(_gucs, roll_back=roll_back) == _tenant_gucs(tenant_b)

                expected_session = _tenant_gucs(tenant_a if roll_back else tenant_b)
                assert _session_gucs() == expected_session
                assert _gucs() == _tenant_gucs(tenant_b)
            finally:
                _reset_context_gucs(token)

    @pytest.mark.parametrize("roll_back", [False, True], ids=["commit", "rollback"])
    def test_balanced_transaction_needs_no_statement(self, roll_back, tenant_a):
        """A context entered and left inside a transaction leaves what was there before it."""

        def in_context():
            with tenant_context(tenant_a.pk):
                return _gucs()

        assert _in_atomic(in_context, roll_back=roll_back) == _tenant_gucs(tenant_a)
        with CaptureQueriesContext(connection) as queries:
            assert get_guc("rls.current_tenant") is None
        assert len(queries) == 1


@pytest.mark.django_db(transaction=True)
class TestTransactionControlInSql:
    """``BEGIN`` / ``ROLLBACK`` / ``COMMIT`` sent as SQL, which Django does not know about."""

    def _in_sql_transaction(self, end, tenant_b):
        """Apply tenant B inside a transaction opened with SQL, then end it with ``end``."""
        with connection.cursor() as cursor:
            cursor.execute("BEGIN")
            assert _gucs() == _tenant_gucs(tenant_b)
            cursor.execute(end)

    @pytest.mark.parametrize("end", ["ROLLBACK", "ABORT"])
    def test_rollback_brings_back_the_values_from_before(self, end, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk):  # committed in autocommit
            token = _set_context_gucs(_context_of(tenant_b))
            try:
                self._in_sql_transaction(end, tenant_b)
            finally:
                _reset_context_gucs(token)
            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == _session_gucs() == _tenant_gucs(tenant_a)
        assert len(queries) == 2  # known to be tenant A again: nothing to apply

    @pytest.mark.parametrize("end", ["COMMIT", "END"])
    def test_commit_keeps_the_values(self, end, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk):
            token = _set_context_gucs(_context_of(tenant_b))
            try:
                self._in_sql_transaction(end, tenant_b)
                with CaptureQueriesContext(connection) as queries:
                    assert _gucs() == _session_gucs() == _tenant_gucs(tenant_b)
            finally:
                _reset_context_gucs(token)
        assert len(queries) == 2


class TestSavepointsTheGuardDidNotSee:
    def test_rollback_to_an_unknown_savepoint_makes_the_values_unknown(
        self,
        tenant_a,
        tenant_b,
        caplog,
    ):
        """A savepoint set with a driver cursor: the guard applies the context again."""
        tokens = []

        def roll_back_to_a_driver_savepoint():
            with connection.connection.cursor() as driver_cursor:
                driver_cursor.execute("SAVEPOINT driver_sp")
            tokens.append(_set_context_gucs(_context_of(tenant_b)))
            _gucs()  # tenant B applied after the savepoint
            with connection.cursor() as cursor:
                cursor.execute("ROLLBACK TO SAVEPOINT Driver_SP")  # unquoted: folded
            with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
                return _session_gucs(), _gucs()

        with tenant_context(tenant_a.pk):
            try:
                session, gucs = _in_atomic(roll_back_to_a_driver_savepoint, roll_back=True)
            finally:
                _reset_context_gucs(tokens.pop())

        assert session == _tenant_gucs(tenant_a)
        assert gucs == _tenant_gucs(tenant_b)
        assert any("held <unknown>" in message for message in caplog.messages)

    def test_failed_savepoint_statement_makes_the_values_unknown(self):
        def fail():
            with pytest.raises(DatabaseError), connection.cursor() as cursor:
                cursor.execute('ROLLBACK TO SAVEPOINT "no_such_savepoint"')
            return _tracker().values

        assert _in_atomic(fail, roll_back=True) is affinity.UNKNOWN


class TestFollow:
    """``_follow()`` and ``_command_of()`` on statements a test database cannot run."""

    @pytest.fixture
    def tracker(self):
        connection.ensure_connection()
        return _tracker()

    @pytest.mark.parametrize("sql", ["COMMIT PREPARED 'tx'", "ROLLBACK PREPARED 'tx'"])
    def test_two_phase_commit_of_another_transaction_changes_nothing(self, tracker, sql):
        tracker.values = ("false", "7")

        affinity._follow(affinity._command_of(sql), sql, connection, tracker)

        assert tracker.values == ("false", "7")

    @pytest.mark.parametrize(
        ("sql", "command"),
        [("  select 1", "SELECT"), ("", ""), (object(), "")],
        ids=["leading-space", "empty", "not-a-string"],
    )
    def test_command_of(self, sql, command):
        assert affinity._command_of(sql) == command


class TestTracking:
    def test_session_the_guard_did_not_see_open_is_unknown(self):
        """No connection_created for it: whatever it holds is cleared outside a context."""
        set_guc("rls.current_tenant", "999")  # a value from no context
        _tracker().raw = None  # another session, as far as it knows

        assert _gucs() == (None, None)

    def test_local_mode_records_nothing(self, tenant_a):
        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local), tenant_context(tenant_a.pk):
            assert _tracker().values is affinity.UNKNOWN

    def test_alias_without_a_connection_is_ignored(self):
        affinity.record("no_such_alias", None)

        with pytest.raises(_Failed), affinity.writing("no_such_alias", None):
            raise _Failed

    def test_connection_without_a_tracker_runs_its_statements_unguarded(self, tenant_a):
        token = _set_context_gucs(_context_of(tenant_a))
        tracker = _tracker()
        delattr(connection, affinity._TRACKER_ATTR)
        try:
            assert _gucs() == (None, None)
        finally:
            setattr(connection, affinity._TRACKER_ATTR, tracker)
            _reset_context_gucs(token)

    @pytest.mark.django_db(transaction=True)
    def test_set_gucs_locally_needs_a_transaction(self):
        with pytest.raises(RuntimeError, match="outside a transaction"):
            affinity.set_gucs(connection, ("true", ""), is_local=True)


class TestSessionReset:
    @pytest.mark.parametrize("statement", ["RESET rls.current_tenant;", "RESET ALL"])
    def test_reset_makes_the_guard_apply_the_context_again(self, statement, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute(statement)
            assert _gucs() == _tenant_gucs(tenant_a)

    def test_reset_of_another_setting_needs_no_statement(self, tenant_a):
        with tenant_context(tenant_a.pk):
            with connection.cursor() as cursor:
                cursor.execute("RESET statement_timeout")
            with CaptureQueriesContext(connection) as queries:
                assert _gucs() == _tenant_gucs(tenant_a)
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
        assert _gucs() == (None, None)

    def test_local_mode_leaves_a_hop_without_context(self, tenant_a, worker):
        local = {**django_settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with override_settings(RLS_TENANTS=local), tenant_context(tenant_a.pk):
            assert _gucs() == _tenant_gucs(tenant_a)
            assert _hop(worker, _gucs) == (None, None)

    def test_alias_outside_rls_databases_does_not_follow(self, replica, tenant_a):
        with tenant_context(tenant_a.pk):
            assert _gucs(replica) == (None, None)


class TestConfiguredAliases:
    """Every alias in ``RLS_TENANTS["DATABASES"]`` follows the context, wherever it was entered."""

    @pytest.fixture
    def with_replica(self, replica):
        databases = {**django_settings.RLS_TENANTS, "DATABASES": [DEFAULT_DB_ALIAS, replica]}
        with override_settings(RLS_TENANTS=databases):
            yield replica

    def test_replica_follows_a_tenant_context(self, sample_orders, tenant_a, with_replica):
        with tenant_context(tenant_a.pk):
            assert _gucs(with_replica) == _tenant_gucs(tenant_a)
            assert _products(with_replica) == _TENANT_A_PRODUCTS
        assert _gucs(with_replica) == (None, None)
        assert _products(with_replica) == []

    def test_admin_context_on_one_alias_applies_on_every_alias(self, with_replica):
        with admin_context(using=with_replica):
            assert _gucs(DEFAULT_DB_ALIAS) == (None, "true")
        assert _gucs(DEFAULT_DB_ALIAS) == (None, None)


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
        assert not any(query["sql"].count("set_config(") > 1 for query in queries)

    def test_middleware_request(self, tenant_a_user):
        middleware = RLSTenantMiddleware(get_response=lambda request: None)
        request = _request_for(tenant_a_user)

        with CaptureQueriesContext(connection) as queries:
            middleware.process_request(request)
            list(Order.objects.all())
            middleware.process_response(request, None)

        assert not any(query["sql"].count("set_config(") > 1 for query in queries)

    def test_for_user(self, tenant_a_user):
        with CaptureQueriesContext(connection) as queries:
            Order.objects.for_user(tenant_a_user).count()

        assert not any(query["sql"].count("set_config(") > 1 for query in queries)


def _request_for(user):
    request = HttpRequest()
    request.user = user
    return request


class TestFailures:
    def test_failed_apply_raises_before_the_statement(self, tenant_a, monkeypatch):
        """Fail closed: the statement never runs with the wrong GUCs."""

        def query_in_context():
            token = _set_context_gucs(_context_of(tenant_a))
            try:
                list(Order.objects.all())
            finally:
                _reset_context_gucs(token)

        reached_the_driver = []

        def record(execute, sql, params, many, context):
            reached_the_driver.append(sql)
            return execute(sql, params, many, context)

        monkeypatch.setattr(
            affinity,
            "_SET_GUCS_SQL",
            "SELECT set_config(%s, %s, %s), no_such_function(%s, %s, %s)",
        )
        with (
            connection.execute_wrapper(record),  # inside the guard: sees what it lets through
            pytest.raises(DatabaseError, match="no_such_function"),
            transaction.atomic(),
        ):
            query_in_context()
        monkeypatch.undo()

        assert any("no_such_function" in sql for sql in reached_the_driver)
        assert not any(Order._meta.db_table in sql for sql in reached_the_driver)
        # Recorded as unknown, so the next statement applies the context.
        token = _set_context_gucs(_context_of(tenant_a))
        try:
            assert _gucs() == _tenant_gucs(tenant_a)
        finally:
            _reset_context_gucs(token)

    def test_failed_transaction_is_left_to_fail_on_its_own(self, tenant_a):
        """No guard statement in an aborted transaction: the caller sees PostgreSQL's error."""

        def select_one():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")

        def in_failed_transaction():
            with pytest.raises(DatabaseError), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
            token = _set_context_gucs(_context_of(tenant_a))
            try:
                with (
                    CaptureQueriesContext(connection) as queries,
                    pytest.raises(DatabaseError, match="current transaction is aborted"),
                ):
                    select_one()
            finally:
                _reset_context_gucs(token)
            return [query["sql"] for query in queries]

        assert _in_atomic(in_failed_transaction, roll_back=True) == ["SELECT 1"]


class TestInstall:
    @pytest.mark.django_db(transaction=True)
    def test_guard_survives_a_user_execute_wrapper_block(self):
        """Django's ``execute_wrapper()`` removes the last wrapper on exit; the guard is first."""

        def passthrough(execute, sql, params, many, context):
            return execute(sql, params, many, context)

        connection.close()
        with connection.execute_wrapper(passthrough):
            connection.ensure_connection()  # connection_created inside the block

        assert connection.execute_wrappers == [affinity._guard]

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
            token = _set_context_gucs(_context_of(tenant_b))
            try:
                assert _gucs() == _tenant_gucs(tenant_b)
            finally:
                _reset_context_gucs(token)


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
        assert _gucs() == (None, None)
