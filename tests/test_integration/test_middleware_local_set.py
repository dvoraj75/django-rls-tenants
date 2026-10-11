"""End-to-end tests for ``RLSTenantMiddleware`` with ``USE_LOCAL_SET=True`` (#95).

Drives real requests through the full middleware stack with ``django.test.Client``.
Before 1.3.2 every authenticated request in this configuration failed with
``RuntimeError: Cannot use SET LOCAL ... outside a transaction`` because Django's
``ATOMIC_REQUESTS`` wraps only the view callable, not ``process_request``.
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.test import Client, override_settings

from django_rls_tenants.exceptions import RLSConfigurationError
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.context import admin_context
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests import admin_urls
from tests.test_app.models import AdminUser, Order

_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
]

_LOCAL_SET = {"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True}


@pytest.fixture
def scoped_web_user(tenant_a):
    """A real ``AUTH_USER_MODEL`` user scoped to tenant A (not an RLS admin)."""
    return AdminUser.objects.create_user(
        username="web_a",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_a,
        rls_admin=False,
    )


@pytest.fixture
def other_web_user(tenant_b):
    """A second scoped web user, for tenant B."""
    return AdminUser.objects.create_user(
        username="web_b",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_b,
        rls_admin=False,
    )


@pytest.fixture(params=[False, True], ids=["no-atomic-requests", "atomic-requests"])
def atomic_requests(request):
    """Run each test with and without Django's own ``ATOMIC_REQUESTS``."""
    previous = connection.settings_dict.get("ATOMIC_REQUESTS", False)
    connection.settings_dict["ATOMIC_REQUESTS"] = request.param
    try:
        yield request.param
    finally:
        connection.settings_dict["ATOMIC_REQUESTS"] = previous


def _assert_request_left_nothing_behind():
    assert connection.in_atomic_block is False
    assert get_guc("rls.current_tenant") is None
    assert get_guc("rls.is_admin") is None
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


@pytest.fixture
def local_set_middleware():
    """Enable the RLS middleware and ``USE_LOCAL_SET=True`` for the request phase.

    Listed *last* in each test's fixture arguments so the data fixtures run
    under the default session-GUC mode (their ``admin_context()`` would need
    an explicit ``atomic()`` under ``SET LOCAL``).
    """
    with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS, RLS_TENANTS=_LOCAL_SET):
        yield


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
class TestMiddlewareLocalSetRequestCycle:
    """The documented pooling configuration works end to end."""

    def test_authenticated_request_sees_its_tenant(
        self, atomic_requests, scoped_web_user, sample_orders, tenant_a, local_set_middleware
    ):
        client = Client()
        client.force_login(scoped_web_user)

        response = client.get("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] == str(tenant_a.pk)
        assert body["is_admin_guc"] == "false"
        assert body["in_atomic_block"] is True
        _assert_request_left_nothing_behind()

    def test_view_exception_still_cleans_up(
        self, atomic_requests, scoped_web_user, local_set_middleware
    ):
        client = Client(raise_request_exception=False)
        client.force_login(scoped_web_user)

        response = client.get("/rls/boom/")

        assert response.status_code == 500
        _assert_request_left_nothing_behind()

    def test_view_exception_rolls_back_the_request_transaction(
        self, atomic_requests, scoped_web_user, local_set_middleware
    ):
        """A raising view loses its writes, exactly like ATOMIC_REQUESTS.

        Django converts the exception into a 500 response inside the chain, so
        the middleware's ``atomic()`` would otherwise commit the partial write;
        ``process_exception`` marks the transaction for rollback.
        """
        client = Client(raise_request_exception=False)
        client.force_login(scoped_web_user)

        response = client.get("/rls/write-then-boom/")

        assert response.status_code == 500
        _assert_request_left_nothing_behind()
        with transaction.atomic(), admin_context():
            assert not Order.objects.filter(product="Doomed").exists()

    def test_explicit_error_response_commits(
        self, atomic_requests, scoped_web_user, local_set_middleware
    ):
        """Returning a 500 without raising commits (ATOMIC_REQUESTS parity)."""
        client = Client()
        client.force_login(scoped_web_user)

        response = client.get("/rls/write-then-500/")

        assert response.status_code == 500
        _assert_request_left_nothing_behind()
        with transaction.atomic(), admin_context():
            assert Order.objects.filter(product="Survivor").exists()

    def test_anonymous_request_is_fail_closed_without_transaction(
        self, atomic_requests, sample_orders, local_set_middleware
    ):
        response = Client().get("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["tenant_guc"] is None
        assert body["products"] == []
        assert body["database_products"] == []
        # No tenant context: only Django's own ATOMIC_REQUESTS wraps the view.
        assert body["in_atomic_block"] is atomic_requests
        _assert_request_left_nothing_behind()

    def test_two_tenants_back_to_back_on_one_connection(
        self, atomic_requests, scoped_web_user, other_web_user, sample_orders, local_set_middleware
    ):
        """Nothing from request one is visible to request two (same thread, same connection)."""
        other = other_web_user
        client_a, client_b = Client(), Client()
        client_a.force_login(scoped_web_user)
        client_b.force_login(other)

        assert client_a.get("/rls/orders/").json()["products"] == ["Widget A1", "Widget A2"]
        assert client_b.get("/rls/orders/").json()["products"] == ["Gadget B1"]
        _assert_request_left_nothing_behind()

    def test_404_handler_can_query_before_request_rollback(
        self, atomic_requests, scoped_web_user, local_set_middleware, monkeypatch
    ):
        monkeypatch.setattr(
            admin_urls, "handler404", "tests.rls_views.database_404_handler", raising=False
        )
        client = Client(raise_request_exception=False)
        client.force_login(scoped_web_user)

        response = client.get("/rls/write-then-404/")

        assert response.status_code == 404
        assert response.json()["tenants"] == 1
        _assert_request_left_nothing_behind()
        with transaction.atomic(), admin_context():
            assert not Order.objects.filter(product="Doomed").exists()

    @pytest.mark.parametrize("handler_after_rls", [False, True])
    @pytest.mark.parametrize(
        "handler_path",
        [
            "tests.rls_views.DatabaseExceptionMiddleware",
            "tests.rls_views.exception_middleware_factory",
        ],
    )
    def test_handled_view_exception_rolls_back(
        self,
        atomic_requests,
        scoped_web_user,
        local_set_middleware,
        handler_after_rls,
        handler_path,
    ):
        middleware = list(_MIDDLEWARE_WITH_RLS)
        middleware.insert(
            len(middleware) if handler_after_rls else 0,
            handler_path,
        )
        client = Client(raise_request_exception=False)
        client.force_login(scoped_web_user)
        with override_settings(MIDDLEWARE=middleware):
            if handler_after_rls:
                with pytest.raises(RLSConfigurationError, match="before RLSTenantMiddleware"):
                    client.get("/rls/write-then-boom/")
                _assert_request_left_nothing_behind()
                return
            response = client.get("/rls/write-then-boom/")

        assert response.status_code == 409
        assert response.json()["tenants"] == 1
        _assert_request_left_nothing_behind()
        with transaction.atomic(), admin_context():
            assert not Order.objects.filter(product="Doomed").exists()
