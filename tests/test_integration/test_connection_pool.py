"""End-to-end tests for Django's psycopg connection pool (``OPTIONS["pool"]``, #96).

Each test borrows from a real ``psycopg_pool.ConnectionPool`` of one connection (the
``pooled`` fixture), so every checkout is the same physical connection and
``pg_backend_pid()`` proves it. Session GUCs survive a return to the pool, so a
connection that the previous request's cleanup did not reach arrives dirty.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from django.conf import settings as django_settings
from django.core.signals import request_finished
from django.db import DatabaseError, connections
from django.test import Client, override_settings

from django_rls_tenants.pool import reset_rls_gucs
from django_rls_tenants.rls.guc import set_guc
from django_rls_tenants.tenants.middleware import _mark_gucs_set
from tests import admin_urls
from tests.test_app.models import AdminUser

pytestmark = [
    pytest.mark.integration,
    pytest.mark.psycopg3_only,
    pytest.mark.django_db(transaction=True),
]

_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
]

_PROBE_URL = "/rls/pool-probe/"
_RECONNECT_URL = "/rls/pool-reconnect/"

_READ_STATE_SQL = (
    "SELECT pg_backend_pid(), current_setting('rls.current_tenant', true), "
    "current_setting('rls.is_admin', true), "
    "current_setting('rls.is_login_request', true)"
)


def _keep_everything(conn):
    """A pool ``reset`` callback that clears nothing, standing in for one of the user's own."""


@pytest.fixture
def web_user_a(tenant_a):
    """A real ``AUTH_USER_MODEL`` user scoped to tenant A."""
    return AdminUser.objects.create_user(
        username="web_a",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_a,
        rls_admin=False,
    )


@pytest.fixture
def web_user_b(tenant_b):
    """A real ``AUTH_USER_MODEL`` user scoped to tenant B."""
    return AdminUser.objects.create_user(
        username="web_b",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_b,
        rls_admin=False,
    )


@contextmanager
def _rls_on(pooled, *, use_local_set=False):
    """Serve requests through ``RLSTenantMiddleware`` with ``pooled`` in ``RLS_TENANTS``."""
    rls_tenants = {
        **django_settings.RLS_TENANTS,
        "DATABASES": ["default", pooled],
        "USE_LOCAL_SET": use_local_set,
    }
    with override_settings(
        MIDDLEWARE=_MIDDLEWARE_WITH_RLS,
        ROOT_URLCONF=admin_urls,
        RLS_TENANTS=rls_tenants,
    ):
        yield


def _probe(client, pooled, url=_PROBE_URL):
    """GET a probe view, then return the pooled connection as a server would at request end."""
    response = client.get(url)
    connections[pooled].close()
    assert response.status_code == 200
    return response.json()


def _raw_state(pooled):
    """Read the backend PID and GUCs straight from the pool, bypassing Django.

    ``pool.connection()`` does not go through ``DatabaseWrapper.connect()``, so
    ``connection_created`` does not fire: this is what the pool holds.

    Returns:
        ``(pid, (tenant, is_admin, login_flag))``, with cleared GUCs as ``None``.
    """
    pool = connections[pooled].pool
    pool.open()
    with pool.connection() as raw:
        pid, *values = raw.execute(_READ_STATE_SQL).fetchone()
    return pid, tuple(value or None for value in values)


def _return_dirty(pooled, tenant_id):
    """Set a tenant's GUCs and a bypass flag, then hand the connection back early.

    Like a request whose view closed its connection before the middleware's
    cleanup, which then reached a different connection (or none).

    Returns:
        The backend PID of the dirty physical connection.
    """
    set_guc("rls.current_tenant", str(tenant_id), using=pooled)
    set_guc("rls.is_admin", "false", using=pooled)
    set_guc("rls.is_login_request", "true", using=pooled)
    with connections[pooled].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        (pid,) = cursor.fetchone()
    connections[pooled].close()
    return pid


class TestPooledCheckout:
    """The ``connection_created`` handler clears every policy GUC on each checkout."""

    def test_anonymous_request_gets_no_gucs_left_by_an_earlier_borrower(
        self,
        pooled,
        sample_orders,
        tenant_a,
    ):
        with _rls_on(pooled):
            dirty_pid = _return_dirty(pooled, tenant_a.pk)
            # Control: the pool really holds the previous borrower's GUCs.
            assert _raw_state(pooled) == (dirty_pid, (str(tenant_a.pk), "false", "true"))

            body = _probe(Client(), pooled)

        assert body == {
            "pid": dirty_pid,
            "tenant_guc": None,
            "is_admin_guc": None,
            "login_flag_guc": None,
            "database_products": [],
        }

    def test_sequential_requests_for_two_tenants_on_one_physical_connection(
        self,
        pooled,
        sample_orders,
        web_user_a,
        web_user_b,
        tenant_a,
        tenant_b,
    ):
        client = Client()
        with _rls_on(pooled):
            client.force_login(web_user_a)
            first = _probe(client, pooled)
            client.force_login(web_user_b)
            second = _probe(client, pooled)
            # Between requests the pool holds a connection the middleware cleared.
            after = _raw_state(pooled)

        assert first["pid"] == second["pid"] == after[0]
        assert (first["tenant_guc"], first["is_admin_guc"]) == (str(tenant_a.pk), "false")
        assert first["database_products"] == ["Widget A1", "Widget A2"]
        assert (second["tenant_guc"], second["is_admin_guc"]) == (str(tenant_b.pk), "false")
        assert second["database_products"] == ["Gadget B1"]
        assert after[1] == (None, None, None)

    def test_request_on_a_dirty_connection_drops_the_stale_bypass_flag(
        self,
        pooled,
        sample_orders,
        web_user_b,
        tenant_a,
        tenant_b,
    ):
        client = Client()
        client.force_login(web_user_b)
        with _rls_on(pooled):
            dirty_pid = _return_dirty(pooled, tenant_a.pk)
            body = _probe(client, pooled)

        assert body["pid"] == dirty_pid
        assert (body["tenant_guc"], body["is_admin_guc"]) == (str(tenant_b.pk), "false")
        assert body["login_flag_guc"] is None
        assert body["database_products"] == ["Gadget B1"]

    def test_mid_request_checkout_gets_the_request_gucs_without_a_stale_flag(
        self,
        pooled,
        sample_orders,
        web_user_b,
        tenant_b,
    ):
        """The view returns its connection early and checks the same one out again."""
        client = Client()
        client.force_login(web_user_b)
        with _rls_on(pooled):
            body = _probe(client, pooled, _RECONNECT_URL)
            _, after = _raw_state(pooled)

        assert (body["tenant_guc"], body["is_admin_guc"]) == (str(tenant_b.pk), "false")
        assert body["login_flag_guc"] is None
        assert body["database_products"] == ["Gadget B1"]
        assert after == (None, None, None)

    @pytest.mark.parametrize("pooled", [{"reset": _keep_everything}], indirect=True)
    def test_clears_even_when_the_pool_has_a_reset(self, pooled, sample_orders, tenant_a):
        with _rls_on(pooled):
            dirty_pid = _return_dirty(pooled, tenant_a.pk)
            # Control: this reset left the GUCs in place.
            assert _raw_state(pooled) == (dirty_pid, (str(tenant_a.pk), "false", "true"))

            body = _probe(Client(), pooled)

        assert body["pid"] == dirty_pid
        assert (body["tenant_guc"], body["is_admin_guc"], body["login_flag_guc"]) == (
            None,
            None,
            None,
        )
        assert body["database_products"] == []

    @pytest.mark.parametrize(
        "pooled",
        [{}, {"close_returns": True}, {"close_returns": True, "reset": reset_rls_gucs}],
        ids=["default", "close-returns", "close-returns-reset"],
        indirect=True,
    )
    def test_failed_clear_discards_the_connection(self, pooled, tenant_a, monkeypatch):
        """A connection that may still be dirty never reaches the borrower or the pool.

        With ``close_returns=True``, closing a psycopg connection returns it to
        the pool instead; it must still be discarded, and returned only once.
        """

        def fail(connection):
            raise DatabaseError("clear failed")

        with _rls_on(pooled):
            dirty_pid = _return_dirty(pooled, tenant_a.pk)
            monkeypatch.setattr("django_rls_tenants.pool._clear_policy_gucs", fail)
            with pytest.raises(DatabaseError, match="clear failed"):
                connections[pooled].ensure_connection()
            monkeypatch.undo()
            assert connections[pooled].connection is None

            pid, gucs = _raw_state(pooled)
            # Returned once, as broken: the pool discarded it and opened another.
            returns_bad = connections[pooled].pool.get_stats().get("returns_bad", 0)

        assert pid != dirty_pid
        assert gucs == (None, None, None)
        assert returns_bad == 1

    def test_use_local_set_request_leaves_the_pooled_connection_clean(
        self,
        pooled,
        sample_orders,
        web_user_a,
        tenant_a,
        tenant_b,
    ):
        client = Client()
        client.force_login(web_user_a)
        with _rls_on(pooled, use_local_set=True):
            dirty_pid = _return_dirty(pooled, tenant_b.pk)
            body = _probe(client, pooled)
            after = _raw_state(pooled)

        assert body["pid"] == dirty_pid
        assert (body["tenant_guc"], body["is_admin_guc"]) == (str(tenant_a.pk), "false")
        assert body["login_flag_guc"] is None
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert after == (dirty_pid, (None, None, None))

    def test_safety_net_does_not_check_out_another_connection(self, pooled, tenant_a):
        """After a failed cleanup the connection is already back in the pool.

        Clearing it through Django would check out the pool's only connection
        and keep it, so the next borrower would wait out the pool timeout.
        """
        with _rls_on(pooled):
            dirty_pid = _return_dirty(pooled, tenant_a.pk)
            _mark_gucs_set()

            request_finished.send(sender=type(self))

            assert connections[pooled].connection is None
            # Still in the pool, still dirty: the next checkout clears it.
            assert _raw_state(pooled) == (dirty_pid, (str(tenant_a.pk), "false", "true"))


class TestResetRlsGucsInThePool:
    """``reset_rls_gucs`` as the pool's ``reset`` callback."""

    @pytest.mark.parametrize("pooled", [{"reset": reset_rls_gucs}], indirect=True)
    def test_returned_connection_is_clean_in_the_pool(self, pooled, tenant_a):
        with _rls_on(pooled):
            dirty_pid = _return_dirty(pooled, tenant_a.pk)

            # No Django checkout, so no connection_created clear: the reset did
            # it, on the same connection (a failed reset would discard it).
            assert _raw_state(pooled) == (dirty_pid, (None, None, None))

    @pytest.mark.parametrize("autocommit", [True, False], ids=["autocommit", "transaction"])
    def test_clears_every_policy_guc_and_leaves_the_connection_idle(self, pooled, autocommit):
        pool = connections[pooled].pool
        pool.open()
        with pool.connection() as raw:
            raw.autocommit = autocommit
            raw.execute(
                "SELECT set_config('rls.current_tenant', '7', false), "
                "set_config('rls.is_admin', 'true', false), "
                "set_config('rls.is_login_request', 'true', false), "
                "set_config('rls.is_preauth_request', 'true', false)"
            )
            if not autocommit:
                raw.commit()

            reset_rls_gucs(raw)

            assert raw.info.transaction_status.name == "IDLE"
            values = raw.execute(
                "SELECT current_setting('rls.current_tenant'), "
                "current_setting('rls.is_admin'), "
                "current_setting('rls.is_login_request'), "
                "current_setting('rls.is_preauth_request')"
            ).fetchone()
            if not autocommit:
                raw.rollback()
            raw.autocommit = True
        assert values == ("", "", "", "")

    def test_keeps_the_time_zone(self, pooled):
        """Unlike ``RESET ALL``: Django sets the time zone once per pooled connection."""
        pool = connections[pooled].pool
        pool.open()
        with pool.connection() as raw:
            original = raw.execute("SHOW TimeZone").fetchone()[0]
            raw.execute("SET TimeZone = 'Asia/Tokyo'")
            try:
                reset_rls_gucs(raw)

                assert raw.execute("SHOW TimeZone").fetchone()[0] == "Asia/Tokyo"
            finally:
                raw.execute("SELECT set_config('TimeZone', %s, false)", [original])
