"""Tests for django_rls_tenants.apps."""

from __future__ import annotations

import pytest
from django.core.signals import request_finished
from django.db import close_old_connections, connection
from django.db.backends.signals import connection_created
from django.test import override_settings

from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.middleware import (
    _clear_gucs_set_flag,
    _mark_gucs_set,
    _were_gucs_set,
)
from django_rls_tenants.tenants.state import set_current_tenant_id

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
        reads settings dynamically. We must invalidate the config cache
        so the singleton re-reads from the overridden settings.
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
            rls_tenants_config._config_cache = None
            request_finished.send(sender=self.__class__)
            # Assert inside override context — GUCs should still be set
            assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.current_tenant") == "42"

        # Restore cache so other tests see the real settings
        rls_tenants_config._config_cache = None

    def test_still_clears_flag_when_use_local_set_true(self):
        """Flag is cleared even when USE_LOCAL_SET=True skips GUC cleanup."""
        _mark_gucs_set()

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            },
        ):
            rls_tenants_config._config_cache = None
            request_finished.send(sender=self.__class__)

        rls_tenants_config._config_cache = None
        assert _were_gucs_set() is False

    def test_clears_flag_even_on_exception(self):
        """_clear_gucs_set_flag() runs even when clear_guc raises.

        Corrupt the underlying psycopg2 connection (without Django
        knowing) so ``clear_guc`` raises ``InterfaceError``.
        The handler should catch it and still clear the flag.
        """
        _mark_gucs_set()
        # Ensure Django has a live connection, then close psycopg2
        # underneath so clear_guc() will fail with InterfaceError.
        connection.ensure_connection()
        connection.connection.close()

        # Handler should catch the exception and still clear the flag
        request_finished.send(sender=self.__class__)

        assert _were_gucs_set() is False
        # Reconnect for subsequent tests / cleanup
        connection.close()

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


@pytest.mark.django_db(transaction=True)
class TestConnectionCreatedSignalHandler:
    """Tests for the connection_created signal handler logic.

    Uses ``transaction=True`` because these tests close and reopen the
    database connection, which breaks Django's test transaction wrapper.

    The handler is a closure inside ``AppConfig.ready()``. We test it
    by forcing a real database reconnection (closing the underlying
    psycopg2 connection and resetting Django's wrapper) so that
    ``connection_created`` fires with ContextVar state active.
    """

    def test_handler_is_connected(self):
        """Verify the connection_created signal has our handler connected."""
        # At least one receiver should be registered (our handler)
        assert len(connection_created.receivers) >= 1

    def test_tenant_context_sets_gucs_on_new_connection(self):
        """When a new connection opens mid-request, GUCs are set from ContextVar."""
        set_current_tenant_id(42)
        _mark_gucs_set()
        try:
            # Force Django to create a new connection by resetting the wrapper.
            connection.ensure_connection()
            connection.connection.close()
            connection.connection = None

            # cursor() triggers reconnection -> connection_created signal
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('rls.current_tenant', true)")
                result = cur.fetchone()

            assert result is not None
            assert result[0] == "42"
        finally:
            set_current_tenant_id(None)
            _clear_gucs_set_flag()

    def test_admin_context_sets_gucs_on_new_connection(self):
        """Admin context (tenant_id=None) sets admin GUCs on new connections."""
        set_current_tenant_id(None)
        _mark_gucs_set()
        try:
            connection.ensure_connection()
            connection.connection.close()
            connection.connection = None

            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('rls.is_admin', true)")
                result = cur.fetchone()

            assert result is not None
            assert result[0] == "true"
        finally:
            set_current_tenant_id(None)
            _clear_gucs_set_flag()

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
