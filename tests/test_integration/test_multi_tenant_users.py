"""End-to-end tests for users who belong to several tenants (#47).

The test ``AUTH_USER_MODEL`` (``AdminUser``) is a ``MultiTenantUser``: its
``rls_tenant_ids`` are its ``Membership`` rows. Real requests go through
``RLSTenantMiddleware`` with ``django.test.Client`` (WSGI) and ``AsyncClient``
(ASGI, the native path and a subclass on the hook path), with session GUCs and
with ``USE_LOCAL_SET=True``, under ``enforce_rls``, so the rows a view reads
are the ones PostgreSQL's policy lets through. The views are in
``tests/rls_views.py``; ``/rls/select-tenant/`` calls ``switch_tenant()`` /
``clear_tenant_selection()``.
"""

from __future__ import annotations

import logging

import pytest
from asgiref.sync import async_to_sync
from django.test import AsyncClient, Client, override_settings

from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests.test_app.models import AdminUser, Membership

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

_RLS = "django_rls_tenants.tenants.middleware.RLSTenantMiddleware"

_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    _RLS,
]

_A_ROWS = ["Widget A1", "Widget A2"]
_B_ROWS = ["Gadget B1"]


def _user(username, *, tenant=None, member_of=(), rls_admin=False):
    """Create an ``AdminUser`` with a default tenant and memberships."""
    user = AdminUser.objects.create_user(
        username=username,
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant,
        rls_admin=rls_admin,
    )
    for member_tenant in member_of:
        Membership.objects.create(user=user, tenant=member_tenant)
    return user


@pytest.fixture
def member(tenant_a, tenant_b):
    """Default tenant A, also a member of tenant B."""
    return _user("member", tenant=tenant_a, member_of=[tenant_b])


@pytest.fixture
def member_without_default(tenant_b):
    """No default tenant, a member of tenant B only."""
    return _user("roaming", member_of=[tenant_b])


@pytest.fixture
def rls_middleware():
    """Enable the RLS middleware for the request phase; listed last in the arguments."""
    with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS):
        yield


@pytest.fixture
def records(caplog):
    """Return a function listing the audit records logged so far."""
    caplog.set_level(logging.INFO, logger=audit.AUDIT_LOGGER_NAME)

    def audit_records():
        return [record for record in caplog.records if record.name == audit.AUDIT_LOGGER_NAME]

    return audit_records


def _logged_in(user, client_class=Client):
    client = client_class()
    client.force_login(user)
    return client


def _get(client, path, **kwargs):
    """GET with a sync ``Client`` or an ``AsyncClient``."""
    if isinstance(client, AsyncClient):
        return async_to_sync(client.get)(path, **kwargs)
    return client.get(path, **kwargs)


def _post(client, path, data):
    """POST with a sync ``Client`` or an ``AsyncClient``."""
    if isinstance(client, AsyncClient):
        return async_to_sync(client.post)(path, data)
    return client.post(path, data)


def _assert_request_left_nothing_behind():
    assert get_guc("rls.current_tenant") is None
    assert get_guc("rls.is_admin") is None
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


@pytest.fixture(params=[Client, AsyncClient], ids=["wsgi", "asgi"])
def client_class(request):
    """Run the test through the WSGI path and the native ASGI path."""
    return request.param


class TestMiddlewareSelection:
    """The middleware runs a member's request in the tenant it selects."""

    def test_no_selection_runs_in_the_default_tenant(
        self, member, sample_orders, tenant_a, client_class, rls_middleware
    ):
        response = _get(_logged_in(member, client_class), "/rls/orders/")

        assert response.status_code == 200
        assert response.json()["database_products"] == _A_ROWS
        assert response.json()["tenant_guc"] == str(tenant_a.pk)
        _assert_request_left_nothing_behind()

    def test_header_selects_another_tenant(
        self, member, sample_orders, tenant_b, client_class, rls_middleware
    ):
        client = _logged_in(member, client_class)

        response = _get(client, "/rls/orders/", headers={"X-Tenant": str(tenant_b.pk)})

        assert response.status_code == 200
        body = response.json()
        assert body["products"] == _B_ROWS
        assert body["database_products"] == _B_ROWS
        assert body["tenant_guc"] == str(tenant_b.pk)
        assert body["is_admin_guc"] == "false"
        _assert_request_left_nothing_behind()
        # Per request: the next one is back in the default tenant.
        assert _get(client, "/rls/orders/").json()["database_products"] == _A_ROWS

    def test_header_of_another_tenant_is_a_403(
        self, member, sample_orders, client_class, records, rls_middleware
    ):
        other_tenant = type(member.tenant).objects.create(name="Tenant C")

        response = _get(
            _logged_in(member, client_class),
            "/rls/orders/",
            headers={"X-Tenant": str(other_tenant.pk)},
        )

        assert response.status_code == 403
        _assert_request_left_nothing_behind()
        denied = [r for r in records() if r.rls_event == audit.CONTEXT_DENIED]
        assert [(r.rls_source, r.rls_reason, r.rls_via) for r in denied] == [
            ("middleware", "tenant_not_selectable", "header"),
        ]
        assert not [r for r in records() if r.rls_event == audit.CONTEXT_ENTERED]

    @pytest.mark.parametrize("value", ["abc", "", "999999"])
    def test_malformed_or_unknown_header_is_a_403(
        self, member, sample_orders, value, client_class, rls_middleware
    ):
        response = _get(
            _logged_in(member, client_class), "/rls/orders/", headers={"X-Tenant": value}
        )

        assert response.status_code == 403
        _assert_request_left_nothing_behind()

    def test_member_without_default_gets_no_context(
        self, member_without_default, sample_orders, client_class, rls_middleware
    ):
        """Fail-closed, but the request is served (a tenant picker page can render)."""
        response = _get(_logged_in(member_without_default, client_class), "/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["database_products"] == []
        assert body["tenant_guc"] is None
        assert body["is_admin_guc"] is None
        _assert_request_left_nothing_behind()

    def test_member_without_default_selects_with_the_header(
        self, member_without_default, sample_orders, tenant_b, client_class, rls_middleware
    ):
        response = _get(
            _logged_in(member_without_default, client_class),
            "/rls/orders/",
            headers={"X-Tenant": str(tenant_b.pk)},
        )

        assert response.status_code == 200
        assert response.json()["database_products"] == _B_ROWS
        _assert_request_left_nothing_behind()

    def test_tenant_admin_ignores_the_header(
        self, sample_orders, tenant_b, client_class, rls_middleware
    ):
        admin = _user("root", rls_admin=True, member_of=[tenant_b])

        response = _get(
            _logged_in(admin, client_class), "/rls/orders/", headers={"X-Tenant": "abc"}
        )

        assert response.status_code == 200
        assert response.json()["is_admin_guc"] == "true"
        assert response.json()["products"] == ["Gadget B1", *_A_ROWS]

    def test_audit_records_the_selected_tenant(
        self, member, sample_orders, tenant_b, records, rls_middleware
    ):
        _logged_in(member).get("/rls/orders/", headers={"X-Tenant": str(tenant_b.pk)})

        entered = [r for r in records() if r.rls_event == audit.CONTEXT_ENTERED]
        assert [(r.rls_source, r.rls_tenant_id, r.rls_user_id) for r in entered] == [
            ("middleware", tenant_b.pk, member.pk),
        ]

    def test_header_turned_off(self, member, sample_orders, tenant_b, rls_middleware):
        settings = {"TENANT_MODEL": "test_app.Tenant", "TENANT_HEADER": None}
        with override_settings(RLS_TENANTS=settings):
            response = _logged_in(member).get(
                "/rls/orders/", headers={"X-Tenant": str(tenant_b.pk)}
            )

        assert response.status_code == 200
        assert response.json()["database_products"] == _A_ROWS


class TestSessionSelection:
    """``switch_tenant()`` selects the tenant of the session's next requests."""

    def test_switch_applies_from_the_next_request(
        self, member, sample_orders, tenant_a, tenant_b, client_class, records, rls_middleware
    ):
        client = _logged_in(member, client_class)

        switched = _post(client, "/rls/select-tenant/", {"tenant": str(tenant_b.pk)})

        assert switched.status_code == 200
        # The request that switches keeps the context it started in.
        assert switched.json() == {
            "selection": str(tenant_b.pk),
            "tenant_id": tenant_a.pk,
            "tenant_guc": str(tenant_a.pk),
        }
        response = _get(client, "/rls/orders/")
        assert response.json()["database_products"] == _B_ROWS
        assert response.json()["tenant_guc"] == str(tenant_b.pk)
        _assert_request_left_nothing_behind()
        (switch,) = [r for r in records() if r.rls_event == audit.TENANT_SWITCHED]
        assert (switch.rls_source, switch.rls_tenant_id, switch.rls_user_id) == (
            "selection",
            tenant_b.pk,
            member.pk,
        )

    def test_header_wins_over_the_session(
        self, member, sample_orders, tenant_a, tenant_b, rls_middleware
    ):
        client = _logged_in(member)
        client.post("/rls/select-tenant/", {"tenant": str(tenant_b.pk)})

        response = client.get("/rls/orders/", headers={"X-Tenant": str(tenant_a.pk)})

        assert response.json()["database_products"] == _A_ROWS
        assert client.get("/rls/orders/").json()["database_products"] == _B_ROWS

    def test_clear_returns_to_the_default(self, member, sample_orders, tenant_b, rls_middleware):
        client = _logged_in(member)
        client.post("/rls/select-tenant/", {"tenant": str(tenant_b.pk)})

        cleared = client.post("/rls/select-tenant/", {"tenant": ""})

        assert cleared.json()["selection"] is None
        assert client.get("/rls/orders/").json()["database_products"] == _A_ROWS

    def test_switch_to_a_tenant_of_another_is_a_403(
        self, member, sample_orders, tenant_b, rls_middleware
    ):
        client = _logged_in(member)
        client.post("/rls/select-tenant/", {"tenant": str(tenant_b.pk)})
        other_tenant = type(member.tenant).objects.create(name="Tenant C")

        refused = client.post("/rls/select-tenant/", {"tenant": str(other_tenant.pk)})

        assert refused.status_code == 403
        # The previous selection stands.
        assert client.get("/rls/orders/").json()["database_products"] == _B_ROWS

    def test_revoked_membership_is_a_403_once_then_the_default(
        self, member, sample_orders, tenant_b, client_class, records, rls_middleware
    ):
        client = _logged_in(member, client_class)
        _post(client, "/rls/select-tenant/", {"tenant": str(tenant_b.pk)})
        Membership.objects.filter(user=member).delete()

        refused = _get(client, "/rls/orders/")
        after = _get(client, "/rls/orders/")

        assert refused.status_code == 403
        assert after.status_code == 200
        assert after.json()["database_products"] == _A_ROWS
        _assert_request_left_nothing_behind()
        denied = [r for r in records() if r.rls_event == audit.CONTEXT_DENIED]
        assert [(r.rls_reason, r.rls_via, r.rls_selected) for r in denied] == [
            ("tenant_not_selectable", "session", str(tenant_b.pk)),
        ]

    def test_logout_ends_the_selection(self, member, sample_orders, tenant_b, rls_middleware):
        client = _logged_in(member)
        client.post("/rls/select-tenant/", {"tenant": str(tenant_b.pk)})

        client.logout()
        client.force_login(member)

        assert client.get("/rls/orders/").json()["database_products"] == _A_ROWS


class TestOtherMiddlewarePaths:
    """``USE_LOCAL_SET=True`` and a subclass that keeps the sync hooks under ASGI."""

    def test_local_set_header_selection(self, member, sample_orders, tenant_b):
        settings = {"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True}
        with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS, RLS_TENANTS=settings):
            client = _logged_in(member)
            selected = client.get("/rls/orders/", headers={"X-Tenant": str(tenant_b.pk)})
            refused = client.get("/rls/orders/", headers={"X-Tenant": "abc"})

        assert selected.status_code == 200
        assert selected.json()["database_products"] == _B_ROWS
        assert selected.json()["in_atomic_block"] is True
        assert refused.status_code == 403
        _assert_request_left_nothing_behind()

    def test_hook_path_under_asgi(self, member, sample_orders, tenant_b):
        middleware = [
            "tests.rls_views.TaggingRLSTenantMiddleware" if path == _RLS else path
            for path in _MIDDLEWARE_WITH_RLS
        ]
        with override_settings(MIDDLEWARE=middleware):
            client = _logged_in(member, AsyncClient)
            selected = async_to_sync(client.get)(
                "/rls/orders/", headers={"X-Tenant": str(tenant_b.pk)}
            )
            refused = async_to_sync(client.get)("/rls/orders/", headers={"X-Tenant": "abc"})

        assert selected.status_code == 200
        assert selected["X-RLS-Hooks"] == "sync"
        assert selected.json()["database_products"] == _B_ROWS
        assert refused.status_code == 403
        _assert_request_left_nothing_behind()


class TestCeleryPropagation:
    """A task enqueued by the request carries the tenant the request selected."""

    def test_selected_tenant_is_captured(self, member, tenant_b, rls_middleware):
        response = _logged_in(member).get(
            "/rls/task-headers/", headers={"X-Tenant": str(tenant_b.pk)}
        )

        assert response.json() == {"rls_tenant_id": tenant_b.pk, "rls_admin": False}


class TestReviewFixes:
    """``Vary`` on the tenant header, and session refusals on the other middleware paths."""

    def test_response_varies_on_the_tenant_header(
        self, member, sample_orders, client_class, rls_middleware
    ):
        response = _get(_logged_in(member, client_class), "/rls/orders/")

        assert "X-Tenant" in response["Vary"]
        assert "Cookie" in response["Vary"]

    def test_no_vary_when_the_header_is_turned_off(
        self, sample_orders, tenant_a, client_class, rls_middleware
    ):
        user = AdminUser.objects.create_user(username="plain", password="pw", tenant=tenant_a)  # noqa: S106
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "TENANT_HEADER": None}
        ):
            response = _get(_logged_in(user, client_class), "/rls/orders/")

        assert "X-Tenant" not in response.get("Vary", "")

    def test_stale_session_on_the_hook_path(self, member, sample_orders, tenant_b):
        middleware = [
            "tests.rls_views.TaggingRLSTenantMiddleware" if path == _RLS else path
            for path in _MIDDLEWARE_WITH_RLS
        ]
        with override_settings(MIDDLEWARE=middleware):
            client = _logged_in(member, AsyncClient)
            _post(client, "/rls/select-tenant/", {"tenant": str(tenant_b.pk)})
            Membership.objects.filter(user=member).delete()
            refused = _get(client, "/rls/orders/")
            after = _get(client, "/rls/orders/")

        assert refused.status_code == 403
        assert after.status_code == 200
        assert after.json()["database_products"] == _A_ROWS
        _assert_request_left_nothing_behind()

    def test_stale_session_under_asgi_with_local_set(self, member, sample_orders, tenant_b):
        settings = {"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True}
        with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS, RLS_TENANTS=settings):
            client = _logged_in(member, AsyncClient)
            _post(client, "/rls/select-tenant/", {"tenant": str(tenant_b.pk)})
            selected = _get(client, "/rls/orders/")
            Membership.objects.filter(user=member).delete()
            refused = _get(client, "/rls/orders/")
            after = _get(client, "/rls/orders/")

        assert selected.json()["database_products"] == _B_ROWS
        assert refused.status_code == 403
        assert after.json()["database_products"] == _A_ROWS
        _assert_request_left_nothing_behind()
