"""End-to-end tests for ``RLSTenantMiddleware`` under ASGI (#58).

Drives real requests through the full middleware stack with ``django.test.AsyncClient``.
Before the #58 fix every authenticated request failed with ``ValueError: <Token ...> was
created in a different Context``: ``MiddlewareMixin.__acall__`` runs ``process_request``
and ``process_response`` / ``process_exception`` in separate ``sync_to_async`` hops,
each in its own copy of the request's ``contextvars.Context``, so the tokens saved by
``process_request`` could not be reset afterwards.

``AsyncClientHandler`` fires ``request_finished`` on a throwaway connection, so the
"nothing left behind" assertions below check the middleware's own cleanup, not the
``request_finished`` safety net.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings as django_settings
from django.test import AsyncClient, override_settings

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests.test_app.models import AdminUser

pytestmark = pytest.mark.django_db

_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
]


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
def admin_web_user(db):
    """A real ``AUTH_USER_MODEL`` user with cross-tenant RLS admin rights."""
    return AdminUser.objects.create_user(
        username="web_admin",
        password="pw",  # noqa: S106  -- test-only credential
        rls_admin=True,
    )


@pytest.fixture
def staff_web_user(tenant_a):
    """A Django superuser scoped to tenant A, like the reporter's admin user."""
    return AdminUser.objects.create_superuser(
        username="staff_a",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_a,
        rls_admin=False,
    )


@pytest.fixture
def rls_middleware():
    """Enable the RLS middleware for the request phase.

    Listed *last* in each test's fixture arguments so the data fixtures run
    before the middleware stack is swapped in.
    """
    with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS):
        yield


def _assert_request_left_nothing_behind():
    assert get_guc("rls.current_tenant") is None
    assert get_guc("rls.is_admin") is None
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


@pytest.mark.integration
class TestMiddlewareAsgiRequestCycle:
    """The default (session GUC) configuration works end to end under ASGI."""

    def test_tenant_request_sees_its_rows(
        self, scoped_web_user, sample_orders, tenant_a, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] == str(tenant_a.pk)
        assert body["is_admin_guc"] == "false"
        _assert_request_left_nothing_behind()

    def test_admin_request_sees_all_rows(self, admin_web_user, sample_orders, rls_middleware):
        client = AsyncClient()
        client.force_login(admin_web_user)

        response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["is_admin_guc"] == "true"
        assert body["products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        _assert_request_left_nothing_behind()

    def test_view_exception_still_cleans_up(self, scoped_web_user, rls_middleware):
        client = AsyncClient(raise_request_exception=False)
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/boom/")

        assert response.status_code == 500
        _assert_request_left_nothing_behind()

    def test_view_exception_is_not_masked(self, scoped_web_user, rls_middleware):
        """The view's own exception reaches the caller, not a cleanup ``ValueError``."""
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with pytest.raises(RuntimeError, match="boom"):
            async_to_sync(client.get)("/rls/boom/")
        _assert_request_left_nothing_behind()

    def test_anonymous_request_sets_nothing(self, sample_orders, rls_middleware):
        response = async_to_sync(AsyncClient().get)("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["products"] == []
        assert body["tenant_guc"] is None
        assert body["is_admin_guc"] is None
        _assert_request_left_nothing_behind()

    def test_admin_changelist_for_scoped_staff_user(
        self, staff_web_user, sample_orders, rls_middleware
    ):
        """The path the #58 reporter hit: the sync Django admin served over ASGI."""
        client = AsyncClient()
        client.force_login(staff_web_user)

        response = async_to_sync(client.get)("/admin/test_app/order/")

        assert response.status_code == 200
        body = response.content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" not in body
        _assert_request_left_nothing_behind()


async def _read_stream(response):
    return b"".join([chunk async for chunk in response.streaming_content]).decode()


@pytest.mark.integration
class TestNativeAsyncPath:
    """The hand-written ``__acall__`` (#38) end to end, with async views."""

    def test_sync_hooks_are_not_used(self, scoped_web_user, sample_orders, rls_middleware):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with (
            patch.object(RLSTenantMiddleware, "process_request", side_effect=AssertionError),
            patch.object(RLSTenantMiddleware, "process_response", side_effect=AssertionError),
        ):
            response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        assert response.json()["products"] == ["Widget A1", "Widget A2"]
        _assert_request_left_nothing_behind()

    def test_async_view_sees_tenant_rows(
        self, scoped_web_user, sample_orders, tenant_a, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/async-orders/")

        assert response.status_code == 200
        assert response.json() == {
            "products": ["Widget A1", "Widget A2"],
            "database_products": ["Widget A1", "Widget A2"],
            "tenant_id": tenant_a.pk,
            "tenant_guc": str(tenant_a.pk),
            "is_admin_guc": "false",
        }
        _assert_request_left_nothing_behind()

    def test_async_view_for_admin_sees_all_rows(
        self, admin_web_user, sample_orders, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(admin_web_user)

        body = async_to_sync(client.get)("/rls/async-orders/").json()

        assert body["products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        assert body["database_products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        assert body["tenant_id"] is None
        assert body["is_admin_guc"] == "true"
        _assert_request_left_nothing_behind()

    def test_async_view_can_widen_to_admin_and_return(
        self, scoped_web_user, sample_orders, tenant_a, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        body = async_to_sync(client.get)("/rls/async-widen/").json()

        assert body == {
            "admin": ["Gadget B1", "Widget A1", "Widget A2"],
            "after": ["Widget A1", "Widget A2"],
            "tenant_guc": str(tenant_a.pk),
        }
        _assert_request_left_nothing_behind()

    def test_streaming_body_runs_after_the_request_context(
        self, scoped_web_user, sample_orders, rls_middleware
    ):
        """Same as WSGI: the body is iterated after cleanup, so it needs its own context."""
        client = AsyncClient()
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/async-stream/")
        assert response.streaming is True
        assert async_to_sync(_read_stream)(response) == "0\n2\n"
        _assert_request_left_nothing_behind()

    def test_strict_mode_async_view(self, scoped_web_user, sample_orders, rls_middleware):
        """The request's context satisfies STRICT_MODE in async views; no context raises."""
        # override_settings, not the ``settings`` fixture: that one is restored after
        # ``rls_middleware`` and would bring the RLS MIDDLEWARE back for later tests.
        strict = {**django_settings.RLS_TENANTS, "STRICT_MODE": True}
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with override_settings(RLS_TENANTS=strict):
            assert async_to_sync(client.get)("/rls/async-orders/").status_code == 200
            with pytest.raises(NoTenantContextError):
                async_to_sync(AsyncClient().get)("/rls/async-orders/")
        _assert_request_left_nothing_behind()

    def test_sync_only_middleware_below_runs_the_sync_path(
        self, scoped_web_user, sample_orders, tenant_a
    ):
        """A sync-only middleware after ours makes Django run ours on its sync path."""
        middleware = [*_MIDDLEWARE_WITH_RLS, "tests.rls_views.SyncOnlyMiddleware"]
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with (
            override_settings(MIDDLEWARE=middleware),
            patch.object(RLSTenantMiddleware, "__acall__", side_effect=AssertionError),
        ):
            response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        assert response.json()["tenant_guc"] == str(tenant_a.pk)
        _assert_request_left_nothing_behind()
