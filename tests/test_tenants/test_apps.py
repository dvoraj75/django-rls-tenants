"""Tests for django_rls_tenants.apps."""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace

import pytest
from django.conf import settings as django_settings
from django.core.signals import request_finished
from django.db import Error, close_old_connections, connection, connections
from django.db.backends.signals import connection_created
from django.test import override_settings

from django_rls_tenants.apps import _is_pooled
from django_rls_tenants.pool import reset_rls_gucs
from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants.middleware import (
    _clear_gucs_set_flag,
    _mark_gucs_set,
    _were_gucs_set,
)
from django_rls_tenants.tenants.state import (
    ADMIN_GUCS,
    ContextGucs,
    _set_context_gucs,
    set_current_tenant_id,
    set_rls_context_active,
)
from tests.session_gucs import session_gucs

pytestmark = pytest.mark.django_db

# Patch target for the module-level singleton.
_CONF_PATCH = "django_rls_tenants.tenants.conf.rls_tenants_config"


@pytest.fixture(autouse=True)
def _isolate_request_finished():
    """Disconnect Django's ``close_old_connections`` during test.

    ``request_finished`` triggers ``close_old_connections``, which
    closes the DB connection and breaks subsequent queries.
    Temporarily disconnecting it lets us test our handler in isolation.
    """
    request_finished.disconnect(close_old_connections)
    yield
    request_finished.connect(close_old_connections)


class TestRequestFinishedSafetyNet:
    """Tests for the request_finished signal handler registered in apps.py.

    The handler is already connected during Django startup, so we just
    fire the ``request_finished`` signal and verify its effects.
    """

    def test_skips_when_gucs_not_set(self):
        """Handler returns immediately when _were_gucs_set() is False."""
        _clear_gucs_set_flag()
        request_finished.send(sender=self.__class__)
        assert get_guc("rls.is_admin") is None

    def test_clears_gucs_when_flag_set(self):
        """Handler clears GUC variables when the thread-local flag is True."""
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "42")
        _mark_gucs_set()

        request_finished.send(sender=self.__class__)

        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_clears_flag_after_cleanup(self):
        """_clear_gucs_set_flag() is always called after successful cleanup."""
        set_guc("rls.is_admin", "true")
        _mark_gucs_set()

        request_finished.send(sender=self.__class__)

        assert _were_gucs_set() is False

    def test_skips_clear_when_use_local_set_true(self):
        """Handler does NOT clear GUCs when USE_LOCAL_SET=True.

        The closure captures the ``rls_tenants_config`` singleton, which
        reloads itself when ``override_settings`` fires ``setting_changed``.
        """
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "42")
        _mark_gucs_set()

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            },
        ):
            request_finished.send(sender=self.__class__)
            # Assert inside override context — GUCs should still be set
            assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.current_tenant") == "42"

    def test_still_clears_flag_when_use_local_set_true(self):
        """Flag is cleared even when USE_LOCAL_SET=True skips GUC cleanup."""
        _mark_gucs_set()

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            },
        ):
            request_finished.send(sender=self.__class__)

        assert _were_gucs_set() is False

    def test_clears_flag_even_on_exception(self):
        """_clear_gucs_set_flag() runs even when clear_guc raises.

        Corrupt the underlying driver connection (without Django
        knowing) so ``clear_guc`` raises ``InterfaceError``.
        The handler should catch it and still clear the flag.
        """
        _mark_gucs_set()
        # Ensure Django has a live connection, then close the driver
        # connection underneath so clear_guc() will fail with InterfaceError.
        connection.ensure_connection()
        connection.connection.close()

        # Handler should catch the exception and still clear the flag
        request_finished.send(sender=self.__class__)

        assert _were_gucs_set() is False
        # Reconnect for subsequent tests / cleanup
        connection.close()

    @pytest.mark.django_db(transaction=True)
    def test_leaves_a_closed_connection_closed(self):
        """A closed session holds no GUCs, and a pooled one is cleared on its next checkout.

        With Django's pool, ``close_old_connections`` has returned the request's
        connection before this handler runs; reopening it would check out
        another pooled connection and keep it.
        """
        _mark_gucs_set()
        connection.close()

        request_finished.send(sender=self.__class__)

        assert connection.connection is None
        assert _were_gucs_set() is False

    def test_safety_net_clears_gucs_on_all_databases(self):
        """Safety-net handler clears GUCs on all configured DATABASES aliases.

        Tests actual GUC effects since the handler uses closure-captured
        references that cannot be patched via module-level mocking.
        """
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "42")
        _mark_gucs_set()

        # The handler reads DATABASES from conf; default config is ["default"]
        request_finished.send(sender=self.__class__)

        # Verify GUCs were cleared on the default database
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None
        assert _were_gucs_set() is False


def _enter_request_context(tenant_id):
    """Set the state ``RLSTenantMiddleware`` sets for a request, on this thread.

    The tenant ``ContextVar`` values, the GUC values the affinity guard applies,
    and the flag that marks this thread as the one that set the GUCs. The autouse
    ``_clear_gucs_after_test`` fixture resets them.
    """
    set_current_tenant_id(tenant_id)
    set_rls_context_active(True)
    _set_context_gucs(ADMIN_GUCS if tenant_id is None else ContextGucs.for_tenant(tenant_id))
    _mark_gucs_set()


@pytest.mark.django_db(transaction=True)
class TestConnectionCreatedSignalHandler:
    """Tests for the connection_created signal handler logic.

    Uses ``transaction=True`` because these tests close and reopen the
    database connection, which breaks Django's test transaction wrapper.

    The handler is a closure inside ``AppConfig.ready()``. We test it
    by forcing a real database reconnection (closing the underlying
    driver connection and resetting Django's wrapper) so that
    ``connection_created`` fires with ContextVar state active.
    """

    def test_handler_is_connected(self):
        """Verify the connection_created signal has our handler connected."""
        # At least one receiver should be registered (our handler)
        assert len(connection_created.receivers) >= 1

    @pytest.mark.parametrize(
        ("tenant_id", "expected"),
        [(42, ("42", "false")), (None, (None, "true"))],
        ids=["tenant", "admin"],
    )
    def test_request_context_is_set_on_new_connection(self, tenant_id, expected):
        """When a new connection opens mid-request, the context's GUCs are set on connect.

        Read with a driver cursor: the affinity guard would set them before a
        statement anyway, so only that shows the handler did.
        """
        _enter_request_context(tenant_id)
        # Force Django to create a new connection by resetting the wrapper.
        connection.ensure_connection()
        connection.connection.close()
        connection.connection = None

        connection.ensure_connection()  # connection_created

        assert session_gucs(connection) == expected

    def test_no_context_skips_guc_setting(self):
        """Without active context, handler does nothing on new connections."""
        _clear_gucs_set_flag()
        set_current_tenant_id(None)

        connection.ensure_connection()
        connection.connection.close()
        connection.connection = None

        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('rls.current_tenant', true)")
            result = cur.fetchone()

        # On a fresh connection with no GUC context, current_setting returns
        # None or empty string depending on PostgreSQL version.
        assert result is not None
        assert result[0] in (None, "")

    def test_flag_without_a_context_gets_nothing_on_a_new_connection(self):
        """The thread's flag alone, with no context values, applies nothing (not admin)."""
        _mark_gucs_set()
        connection.ensure_connection()
        connection.connection.close()
        connection.connection = None

        connection.ensure_connection()  # connection_created

        assert session_gucs(connection) == (None, None)


def _on_new_thread(func):
    """Run ``func`` on a new thread in a copy of the current context; close its connections.

    Like a ``sync_to_async(..., thread_sensitive=False)`` hop: the ``ContextVar``
    values come along, the database connection is the new thread's own.
    """

    def call():
        try:
            return func()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(contextvars.copy_context().run, call).result()


def _read_gucs():
    return get_guc("rls.current_tenant"), get_guc("rls.is_admin")


_STALE_GUCS_SQL = (
    "SELECT set_config('rls.current_tenant', '999', false), "
    "set_config('rls.is_admin', 'true', false)"
)


def _arrive_with(sql, alias="default"):
    """Make this thread's next ``alias`` connection run ``sql`` before ``connection_created``.

    A connection reused from a pool can carry a previous borrower's session state.
    """
    conn = connections[alias]
    init_connection_state = conn.init_connection_state

    def init_then_run():
        init_connection_state()
        with conn.connection.cursor() as cursor:
            cursor.execute(sql)

    conn.init_connection_state = init_then_run


def _read_gucs_on_dirty_connection():
    _arrive_with(_STALE_GUCS_SQL)
    return _read_gucs()


def _read_gucs_on_dirty_replica_connection(*, on_connect=False):
    """The same on a second alias, registered on this thread only.

    ``on_connect`` reads with a driver cursor right after the connection opens,
    before the affinity guard applies anything.
    """
    default = connections["default"]
    replica = type(default)(default.settings_dict, alias="replica")
    connections["replica"] = replica
    try:
        _arrive_with(_STALE_GUCS_SQL, alias="replica")
        if on_connect:
            return session_gucs(replica)
        return get_guc("rls.current_tenant", using="replica"), get_guc(
            "rls.is_admin", using="replica"
        )
    finally:
        del connections["replica"]
        replica.close()


_STALE_POLICY_GUCS_SQL = (
    "SELECT set_config('rls.current_tenant', '999', false), "
    "set_config('rls.is_admin', 'true', false), "
    "set_config('rls.is_login_request', 'true', false)"
)


def _read_policy_gucs():
    """The tenant and admin GUCs plus one of ``ProtectedUser``'s ``extra_bypass_flags``."""
    return (*_read_gucs(), get_guc("rls.is_login_request"))


def _read_policy_gucs_on_dirty_connection():
    _arrive_with(_STALE_POLICY_GUCS_SQL)
    return _read_policy_gucs()


def _connect_on_broken_connection():
    conn = connections["default"]
    init_connection_state = conn.init_connection_state

    def init_then_break():
        init_connection_state()
        conn.connection.close()

    conn.init_connection_state = init_then_break
    conn.ensure_connection()


def _read_session_gucs_on_connect():
    """Open this thread's connection and read its GUCs before any statement runs."""
    conn = connections["default"]
    conn.ensure_connection()
    return session_gucs(conn)


class TestConnectionCreatedOnAnotherThread:
    """#130, #41: a connection opened on another thread starts clean, then inherits the context.

    The request's cleanup clears that thread's connections only. A connection
    opened on another thread holding a copy of the request's context (Django's
    ``thread_sensitive=False`` error-page hop under ASGI) is cleared when it
    opens and gets nothing from the handler. The affinity guard then applies the
    context before each statement run in that copy of it, and clears it again
    before the first one run without it (``TestAffinityGuardOnHops``).
    """

    @pytest.mark.parametrize(
        ("tenant_id", "inherited"),
        [(42, ("42", "false")), (None, (None, "true"))],
        ids=["tenant", "admin"],
    )
    def test_connection_starts_clean_and_inherits_the_context(
        self,
        tenant_id,
        inherited,
        caplog,
    ):
        _enter_request_context(tenant_id)

        with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
            assert _on_new_thread(_read_session_gucs_on_connect) == (None, None)
            assert _on_new_thread(_read_gucs) == inherited

        assert any("outside the request's thread" in m for m in caplog.messages)

    def test_stale_gucs_on_a_reused_connection_are_cleared(self):
        # Control: outside a request the handler leaves a connection alone, so the
        # simulated reused connection really does arrive with both GUCs set.
        assert _on_new_thread(_read_gucs_on_dirty_connection) == ("999", "true")

        _enter_request_context(42)

        def read_on_connect():
            _arrive_with(_STALE_GUCS_SQL)
            return _read_session_gucs_on_connect()

        assert _on_new_thread(read_on_connect) == (None, None)
        assert _on_new_thread(_read_gucs_on_dirty_connection) == ("42", "false")

    def test_clear_runs_on_the_new_connections_alias(self):
        databases = {**django_settings.RLS_TENANTS, "DATABASES": ["default", "replica"]}
        with override_settings(RLS_TENANTS=databases):
            # Control, as above: the replica connection really arrives with the GUCs.
            assert _on_new_thread(_read_gucs_on_dirty_replica_connection) == ("999", "true")

            _enter_request_context(42)

            read_on_connect = partial(_read_gucs_on_dirty_replica_connection, on_connect=True)
            assert _on_new_thread(read_on_connect) == (None, None)
            assert _on_new_thread(_read_gucs_on_dirty_replica_connection) == ("42", "false")

    def test_failed_clear_is_logged_not_raised(self, caplog):
        _enter_request_context(42)

        with caplog.at_level(logging.WARNING, logger="django_rls_tenants"):
            _on_new_thread(_connect_on_broken_connection)

        assert any("Failed to clear RLS GUCs" in m for m in caplog.messages)

    def test_clear_covers_every_policy_guc(self):
        """Bypass flags a reused connection carries go too; the guard restores only the context."""
        _enter_request_context(42)

        assert _on_new_thread(_read_policy_gucs_on_dirty_connection) == ("42", "false", None)

    def test_owner_thread_still_gets_gucs(self):
        """The thread that set the flag keeps the lazily-opened-connection behaviour."""

        def owner():
            _enter_request_context(42)
            return _read_session_gucs_on_connect()

        assert _on_new_thread(owner) == ("42", "false")


@pytest.fixture
def pooled_connections(monkeypatch):
    """Treat every new connection as a checkout from Django's connection pool.

    The real pool needs psycopg 3 (``test_integration/test_connection_pool.py``);
    this runs the handler's pooled branch on either driver.
    """
    monkeypatch.setattr("django_rls_tenants.apps._is_pooled", lambda connection: True)


class TestConnectionCreatedOnPooledConnection:
    """#96: a connection checked out of a pool starts without any policy GUC.

    It may carry the session GUCs of its previous borrower, whose cleanup did
    not reach it. ``_arrive_with`` simulates that borrower.
    """

    def test_control_unpooled_connection_outside_a_request_is_left_alone(self):
        assert _on_new_thread(_read_policy_gucs_on_dirty_connection) == ("999", "true", "true")

    def test_checkout_outside_a_request_clears_every_policy_guc(self, pooled_connections, caplog):
        with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
            assert _on_new_thread(_read_policy_gucs_on_dirty_connection) == (None, None, None)

        assert any("checked out of a connection pool" in m for m in caplog.messages)

    @pytest.mark.parametrize(
        ("tenant_id", "expected"),
        [(42, ("42", "false", None)), (None, (None, "true", None))],
        ids=["tenant", "admin"],
    )
    def test_checkout_on_the_request_thread_gets_the_request_gucs_only(
        self,
        pooled_connections,
        tenant_id,
        expected,
    ):
        def owner():
            _enter_request_context(tenant_id)
            return _read_policy_gucs_on_dirty_connection()

        assert _on_new_thread(owner) == expected

    def test_checkout_on_another_thread_clears_every_policy_guc(self, pooled_connections):
        """Every stale GUC goes; the guard then applies the copied context's own."""
        _enter_request_context(42)

        assert _on_new_thread(_read_policy_gucs_on_dirty_connection) == ("42", "false", None)

    def test_alias_outside_rls_databases_is_left_alone(self, pooled_connections):
        assert _on_new_thread(_read_gucs_on_dirty_replica_connection) == ("999", "true")

    def test_clear_runs_on_the_connection_that_connected(self, pooled_connections):
        """Not on the thread's registered wrapper: ``create_connection()`` makes another one."""

        def on_separate_wrapper():
            other = connections.create_connection("default")
            init_connection_state = other.init_connection_state

            def init_then_dirty():
                init_connection_state()
                with other.connection.cursor() as cursor:
                    cursor.execute(_STALE_POLICY_GUCS_SQL)

            other.init_connection_state = init_then_dirty
            try:
                with other.cursor() as cursor:
                    cursor.execute(
                        "SELECT current_setting('rls.current_tenant', true), "
                        "current_setting('rls.is_login_request', true)"
                    )
                    values = tuple(value or None for value in cursor.fetchone())
            finally:
                other.close()
            return values, connections["default"].connection is None

        assert _on_new_thread(on_separate_wrapper) == ((None, None), True)

    def test_clear_is_one_statement(self, pooled_connections):
        def connect_and_log():
            conn = connections["default"]
            conn.force_debug_cursor = True
            conn.ensure_connection()
            return [query["sql"] for query in conn.queries_log]

        statements = _on_new_thread(connect_and_log)

        assert len(statements) == 1
        assert statements[0].count("set_config(") == 4

    def test_failed_clear_fails_the_checkout_and_drops_the_connection(
        self,
        pooled_connections,
        caplog,
    ):
        """A connection that may still be dirty is never handed to the borrower."""

        def connect():
            try:
                _connect_on_broken_connection()
            except Error as exc:
                return type(exc), connections["default"].connection
            return None, connections["default"].connection

        with caplog.at_level(logging.WARNING, logger="django_rls_tenants"):
            error, raw_connection = _on_new_thread(connect)

        assert error is not None
        assert raw_connection is None
        assert any(
            "Failed to clear RLS GUCs" in m
            and "checked out of a connection pool; discarding it" in m
            for m in caplog.messages
        )


class TestIsPooled:
    """``_is_pooled`` follows Django's own test for an enabled pool."""

    @pytest.mark.parametrize(
        ("pool", "expected"),
        [
            (True, True),
            ({"max_size": 4}, True),
            # The checkout clear runs even when the pool has a reset callback.
            ({"reset": reset_rls_gucs}, True),
            ({}, False),
            (False, False),
            (None, False),
        ],
    )
    def test_follows_options_pool(self, pool, expected):
        assert _is_pooled(SimpleNamespace(settings_dict={"OPTIONS": {"pool": pool}})) is expected

    def test_unpooled_test_connection(self):
        assert _is_pooled(connection) is False
