"""Tests for django_rls_tenants.contrib.ninja (issue #167).

Django Ninja authenticates inside the operation, after every Django middleware, so
an API-key client reaches ``RLSTenantMiddleware`` as an anonymous user. The APIs
under test (``tests/ninja_views.py``, routed by ``tests/ninja_urls.py``) authenticate
with an API key (``X-User: <username>``); the test settings have no
``RLSTenantMiddleware``, so every context a request runs in is the API's.

The suite splits into:

* the order of the steps (``add_decorator`` modes) and the guard's rule (#150);
* ``@pytest.mark.integration`` request-cycle tests under ``enforce_rls``: row isolation,
  tenant selection, the context's exit on every path, error rendering inside the
  context, the schema helpers, ``STRICT_MODE``, both GUC modes, ASGI and audit records.

Most request tests run as three flavours: a ``def`` operation, an ``async def``
operation, and a ``def`` operation served under ASGI.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import get_args

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.test import AsyncClient, Client, RequestFactory, override_settings
from ninja.errors import ValidationError as NinjaValidationError

from django_rls_tenants.contrib.ninja import (
    RLSTenantGuard,
    RLSTenantModelSchema,
    rls_save,
)
from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.audit import AUDIT_LOGGER_NAME
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.selection import NOT_SELECTABLE_MESSAGE
from django_rls_tenants.tenants.state import (
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)
from tests import ninja_views
from tests.ninja_views import ItemIn, api, guard
from tests.session_gucs import session_gucs
from tests.test_app.models import AdminUser, Membership, Order, OrderItem, Tenant
from tests.test_external_app.models import Book

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.ninja_urls")]

_NO_TENANT_DETAIL = "Your account is not assigned to a tenant."


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _api_user(username, *, tenant=None, rls_admin=False):
    """Create an ``AUTH_USER_MODEL`` user; its username is its API key."""
    return AdminUser.objects.create_user(
        username=username,
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant,
        rls_admin=rls_admin,
    )


@pytest.fixture
def user_a(tenant_a):
    """An API user scoped to tenant A."""
    return _api_user("api_a", tenant=tenant_a)


@pytest.fixture
def user_b(tenant_b):
    """An API user scoped to tenant B."""
    return _api_user("api_b", tenant=tenant_b)


@pytest.fixture
def api_admin(db):
    """An API user with cross-tenant RLS admin rights."""
    return _api_user("api_admin", rls_admin=True)


@pytest.fixture
def tenantless_user(db):
    """An API user that is neither a tenant admin nor assigned to a tenant."""
    return _api_user("api_nobody")


@pytest.fixture
def member_of(db):
    """Make ``tenants`` the ones a user is a member of (``Membership``, ``rls_tenant_ids``)."""

    def add(user, *tenants):
        Membership.objects.filter(user=user).delete()
        for tenant in tenants:
            Membership.objects.create(user=user, tenant=tenant)

    return add


@pytest.fixture(autouse=True)
def _clear_events():
    """``ninja_views.EVENTS`` records the order of the steps of one test's requests."""
    ninja_views.EVENTS.clear()
    yield
    ninja_views.EVENTS.clear()


def _assert_context_closed():
    """Nothing the request entered is left on this thread or its connection."""
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False
    assert _get_context_gucs() is None
    assert session_gucs() == (None, None)


class Flavour:
    """How a test reaches the API: operation kind and the test client."""

    def __init__(self, name, *, is_async, via_asgi):
        self.name = name
        self.is_async = is_async
        self.via_asgi = via_asgi

    def path(self, operation, base="/api"):
        """The URL of ``operation``; the ``async def`` twin of each is named ``a<operation>``."""
        return f"{base}/{'a' if self.is_async else ''}{operation}"

    def call(self, method, path, *, user=None, headers=None, body=None):
        """Send a request; ``user`` is named by the ``X-User`` header (the API key)."""
        sent = {**({"X-User": user.username} if user else {}), **(headers or {})}
        extra = {} if body is None else {"data": body, "content_type": "application/json"}
        if self.via_asgi:
            send = getattr(AsyncClient(raise_request_exception=False), method)
            return async_to_sync(send)(path, headers=sent, **extra)
        return getattr(Client(raise_request_exception=False), method)(path, headers=sent, **extra)

    def get(self, operation, user=None, *, headers=None, base="/api"):
        return self.call("get", self.path(operation, base), user=user, headers=headers)

    def post(self, operation, user, body, *, headers=None, base="/api"):
        return self.call("post", self.path(operation, base), user=user, headers=headers, body=body)


@pytest.fixture(
    params=[
        Flavour("sync", is_async=False, via_asgi=False),
        Flavour("async", is_async=True, via_asgi=True),
        Flavour("sync-under-asgi", is_async=False, via_asgi=True),
    ],
    ids=lambda flavour: flavour.name,
)
def flavour(request):
    """A ``def`` operation, an ``async def`` operation, a ``def`` operation under ASGI."""
    return request.param


@pytest.fixture(
    params=[
        Flavour("sync", is_async=False, via_asgi=False),
        Flavour("async", is_async=True, via_asgi=True),
    ],
    ids=lambda flavour: flavour.name,
)
def twin(request):
    """Only the flavours that have an operation of their own kind (not sync under ASGI)."""
    return request.param


def _products(response):
    return sorted(item["product"] for item in response.json())


def _request_of(user=None):
    """A request whose ``auth`` is ``user`` (as Ninja sets it after authenticating)."""
    request = RequestFactory().get("/")
    request.auth = user
    request.user = user if user is not None else AnonymousUser()
    return request


def _scoped(tenant_id=3):
    """An authenticated, tenant-scoped stand-in user."""
    return SimpleNamespace(
        is_authenticated=True, is_tenant_admin=False, rls_tenant_id=tenant_id, pk=7
    )


def _admin():
    """An authenticated tenant-admin stand-in user."""
    return SimpleNamespace(is_authenticated=True, is_tenant_admin=True, rls_tenant_id=None, pk=8)


# ---------------------------------------------------------------------------
# Isolation: the context is the request's tenant
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTenantIsolation:
    def test_orm_and_database_see_only_the_tenant(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a
    ):
        response = flavour.get("state", user_a)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_id"] == tenant_a.pk
        assert body["context_active"] is True
        assert body["tenant_guc"] == str(tenant_a.pk)
        assert body["is_admin_guc"] == "false"
        _assert_context_closed()

    def test_lazy_queryset_is_serialized_inside_the_context(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a
    ):
        response = flavour.get("lazy", user_a)

        assert response.status_code == 200, response.content
        assert _products(response) == ["Widget A1", "Widget A2"]
        assert all(item["seen_tenant"] == tenant_a.pk for item in response.json())
        _assert_context_closed()

    def test_auth_runs_before_the_entry_and_serialization_before_the_exit(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a
    ):
        flavour.get("lazy", user_a)

        assert [
            ("auth", None),
            ("view", tenant_a.pk),
            ("serialize", tenant_a.pk),
            ("serialize", tenant_a.pk),
        ] == ninja_views.EVENTS
        _assert_context_closed()

    def test_the_response_does_not_show_the_tenant(
        self, enforce_rls, flavour, user_a, sample_orders
    ):
        response = flavour.get("lazy", user_a)

        assert all("tenant" not in item and "tenant_id" not in item for item in response.json())

    def test_tenant_admin_gets_the_admin_context(
        self, enforce_rls, flavour, api_admin, sample_orders
    ):
        response = flavour.get("state", api_admin)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        assert body["is_admin_guc"] == "true"
        assert body["tenant_id"] is None
        _assert_context_closed()

    def test_another_tenants_user_sees_their_own_rows(
        self, enforce_rls, flavour, user_a, user_b, sample_orders
    ):
        assert flavour.get("state", user_a).json()["products"] == ["Widget A1", "Widget A2"]
        assert flavour.get("state", user_b).json()["products"] == ["Gadget B1"]
        _assert_context_closed()

    @pytest.mark.parametrize(
        "operation", ["outer/state", "outer/inner/state", "outer/inner/astate"]
    )
    def test_routers_mounted_on_the_api_are_covered(
        self, enforce_rls, user_a, sample_orders, operation
    ):
        send = Flavour("any", is_async=False, via_asgi=operation.endswith("astate"))

        response = send.call("get", f"/api/{operation}", user=user_a)

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        _assert_context_closed()


# ---------------------------------------------------------------------------
# The order of the steps: add_decorator() modes
# ---------------------------------------------------------------------------


def _operations(*, router_api):
    return [
        operation
        for bound in router_api._get_bound_routers()
        for path_view in bound.path_operations.values()
        for operation in path_view.operations
    ]


class TestDecoratorModes:
    """Ninja's ``mode="view"`` wraps ``run()`` and ``mode="operation"`` wraps the view function.

    The names read the other way round, so a Ninja release that swaps them would
    exit the context before serialization, or enter it before authentication.
    """

    def test_every_route_of_every_router_is_bound(self):
        operations = _operations(router_api=api)

        paths = {operation.view_func.__name__ for operation in operations}
        assert {"state", "outer_state", "inner_state", "inner_astate"} <= paths

    def test_the_exit_wrapper_is_what_run_became(self):
        for operation in _operations(router_api=api):
            qualname = operation.run.__code__.co_qualname
            assert qualname.startswith("RLSTenantNinjaAPI._rls_own_exit"), operation.view_func
            wrapped = operation.run.__wrapped__
            assert wrapped.__func__ is type(operation).run

    def test_the_entry_wrapper_is_what_the_view_function_became(self):
        for operation in _operations(router_api=api):
            qualname = operation.view_func.__code__.co_qualname
            assert qualname.startswith("RLSTenantNinjaAPI._rls_enter"), operation.view_func

    def test_the_guard_is_inside_the_entry(self):
        for operation in _operations(router_api=api):
            guarded = operation.view_func.__wrapped__
            assert guarded.__code__.co_qualname.startswith("RLSTenantGuard.__call__")


# ---------------------------------------------------------------------------
# Errors and the context's exit
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestContextExit:
    def test_a_handled_error_is_rendered_inside_the_context(
        self, enforce_rls, twin, user_a, sample_orders, tenant_a
    ):
        response = twin.get("boom", user_a)

        assert response.status_code == 418
        body = response.json()
        assert body["tenant_id"] == tenant_a.pk
        assert body["context_active"] is True
        _assert_context_closed()

    def test_a_handled_errors_page_reads_only_the_tenants_rows(
        self, enforce_rls, user_a, sample_orders
    ):
        body = Flavour("sync", is_async=False, via_asgi=False).get("boom", user_a).json()

        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] is not None

    def test_a_handled_error_under_asgi_is_rendered_inside_the_context(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        flavour = Flavour("sync-under-asgi", is_async=False, via_asgi=True)

        body = flavour.get("boom", user_a).json()

        assert body["tenant_id"] == tenant_a.pk
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        _assert_context_closed()

    def test_an_unhandled_error_closes_the_context(
        self, enforce_rls, flavour, user_a, sample_orders
    ):
        response = flavour.get("crash", user_a)

        assert response.status_code == 500
        _assert_context_closed()

    def test_the_next_request_starts_without_a_context(
        self, enforce_rls, flavour, user_a, sample_orders
    ):
        assert flavour.get("state", user_a).status_code == 200

        anonymous = flavour.call("get", "/open/guarded")

        assert anonymous.status_code == 401
        _assert_context_closed()

    def test_an_outer_context_is_restored(
        self, enforce_rls, user_a, sample_orders, tenant_a, tenant_b
    ):
        with tenant_context(tenant_b.pk):
            response = Flavour("sync", is_async=False, via_asgi=False).get("state", user_a)

            assert response.json()["products"] == ["Widget A1", "Widget A2"]
            assert get_current_tenant_id() == tenant_b.pk
        _assert_context_closed()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuthentication:
    def test_a_failed_auth_is_a_401_and_the_view_never_runs(
        self, enforce_rls, flavour, user_a, sample_orders, caplog
    ):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        stranger = SimpleNamespace(username="nobody")

        response = flavour.get("lazy", stranger)

        assert response.status_code == 401
        assert ("view", get_current_tenant_id()) not in ninja_views.EVENTS
        assert [event for event, _ in ninja_views.EVENTS] == ["auth"]
        assert [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME] == []
        _assert_context_closed()

    def test_a_request_without_a_key_is_a_401(self, enforce_rls, flavour, sample_orders):
        response = flavour.get("state")

        assert response.status_code == 401
        _assert_context_closed()

    def test_an_api_without_auth_enters_nothing_for_an_anonymous_request(
        self, enforce_rls, sample_orders, caplog
    ):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        response = Client().get("/open/state")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["products"] == []
        assert body["database_products"] == []
        assert body["context_active"] is False
        assert [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME] == []
        _assert_context_closed()

    def test_an_api_without_auth_uses_the_session_user(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        client = Client()
        client.force_login(user_a)

        response = client.get("/open/state")

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        assert response.json()["tenant_id"] == tenant_a.pk
        _assert_context_closed()

    def test_tenantless_non_admin_gets_a_403(
        self, enforce_rls, flavour, tenantless_user, sample_orders
    ):
        response = flavour.get("state", tenantless_user)

        assert response.status_code == 403
        assert response.json() == {"detail": _NO_TENANT_DETAIL}
        _assert_context_closed()

    def test_the_403_does_not_show_the_hint(self, enforce_rls, tenantless_user, caplog):
        caplog.set_level(logging.WARNING, logger="django_rls_tenants")

        response = Client().get("/api/state", headers={"X-User": tenantless_user.username})

        assert "rls_tenant_id" not in response.content.decode()
        assert any(
            "not a tenant admin and has no tenant" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Tenant selection (#47): the header and the session
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTenantSelection:
    def test_the_header_selects_one_of_the_members_tenants(
        self, enforce_rls, flavour, user_a, member_of, sample_orders, tenant_a, tenant_b
    ):
        member_of(user_a, tenant_a, tenant_b)

        selected = flavour.get("state", user_a, headers={"X-Tenant": str(tenant_b.pk)})
        own = flavour.get("state", user_a)

        assert selected.status_code == 200, selected.content
        assert selected.json()["database_products"] == ["Gadget B1"]
        assert selected.json()["tenant_id"] == tenant_b.pk
        assert own.json()["database_products"] == ["Widget A1", "Widget A2"]
        _assert_context_closed()

    def test_a_member_without_a_default_must_select(
        self, enforce_rls, flavour, tenantless_user, member_of, sample_orders, tenant_b
    ):
        member_of(tenantless_user, tenant_b)

        unselected = flavour.get("state", tenantless_user)
        selected = flavour.get("state", tenantless_user, headers={"X-Tenant": str(tenant_b.pk)})

        assert unselected.status_code == 403
        assert unselected.json() == {"detail": _NO_TENANT_DETAIL}
        assert selected.status_code == 200, selected.content
        assert selected.json()["database_products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_the_session_selects(self, enforce_rls, user_a, member_of, sample_orders, tenant_b):
        member_of(user_a, tenant_b)
        client = Client()
        client.force_login(user_a)
        session = client.session
        session["rls_tenant"] = str(tenant_b.pk)
        session.save()

        response = client.get("/api/state", headers={"X-User": user_a.username})

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_the_session_selection_of_another_user_is_ignored(
        self, enforce_rls, user_a, user_b, member_of, sample_orders, tenant_a
    ):
        client = Client()
        client.force_login(user_a)
        session = client.session
        session["rls_tenant"] = str(tenant_a.pk)  # A's selection
        session.save()
        member_of(user_b, tenant_a)

        response = client.get("/api/state", headers={"X-User": user_b.username})

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]  # B's default
        assert client.session["rls_tenant"] == str(tenant_a.pk)

    @pytest.mark.parametrize("selected", ["other", "missing", "junk"])
    def test_a_tenant_that_cannot_be_selected_is_a_403(
        self, enforce_rls, flavour, user_a, member_of, sample_orders, tenant_b, selected
    ):
        member_of(user_a)
        value = {"other": str(tenant_b.pk), "missing": "999999", "junk": "abc"}[selected]

        response = flavour.get("state", user_a, headers={"X-Tenant": value})

        assert response.status_code == 403
        assert response.json() == {"detail": NOT_SELECTABLE_MESSAGE}
        assert "X-Tenant" in response["Vary"]
        assert [event for event, _ in ninja_views.EVENTS] == ["auth"]
        _assert_context_closed()

    def test_the_response_varies_on_the_header(self, enforce_rls, flavour, user_a, sample_orders):
        response = flavour.get("state", user_a)

        assert response.status_code == 200, response.content
        assert "X-Tenant" in response["Vary"]

    def test_the_header_name_comes_from_the_setting(
        self, enforce_rls, flavour, user_a, member_of, sample_orders, tenant_b
    ):
        member_of(user_a)
        with override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "TENANT_HEADER": "X-Org"}):
            refused = flavour.get("state", user_a, headers={"X-Org": str(tenant_b.pk)})
            ignored = flavour.get("state", user_a, headers={"X-Tenant": str(tenant_b.pk)})

        assert refused.status_code == 403
        assert refused.json() == {"detail": NOT_SELECTABLE_MESSAGE}
        assert "X-Org" in refused["Vary"]
        assert "X-Tenant" not in refused["Vary"]
        assert ignored.status_code == 200
        assert "X-Org" in ignored["Vary"]

    def test_a_tenant_admins_header_is_ignored(
        self, enforce_rls, flavour, api_admin, sample_orders
    ):
        response = flavour.get("state", api_admin, headers={"X-Tenant": "abc"})

        assert response.status_code == 200, response.content
        assert response.json()["is_admin_guc"] == "true"
        _assert_context_closed()


# ---------------------------------------------------------------------------
# RLSTenantGuard (#150)
# ---------------------------------------------------------------------------


class TestGuardRule:
    """``has_permission()`` on its own, with the context entered by the test."""

    def test_an_unauthenticated_request_is_refused(self):
        assert RLSTenantGuard().has_permission(_request_of(None)) is False

    def test_no_context_is_refused(self):
        assert RLSTenantGuard().has_permission(_request_of(_scoped(3))) is False

    def test_the_users_own_tenant_is_allowed(self):
        with tenant_context(3):
            assert RLSTenantGuard().has_permission(_request_of(_scoped(3))) is True

    def test_another_tenant_is_refused(self):
        with tenant_context(4):
            assert RLSTenantGuard().has_permission(_request_of(_scoped(3))) is False

    def test_the_admin_context_is_refused_for_a_non_admin(self):
        with admin_context():
            assert RLSTenantGuard().has_permission(_request_of(_scoped(3))) is False

    def test_a_tenant_admin_is_allowed_in_any_context(self):
        request = _request_of(_admin())
        with admin_context():
            assert RLSTenantGuard().has_permission(request) is True
        with tenant_context(4):
            assert RLSTenantGuard().has_permission(request) is True

    def test_a_tenant_admin_needs_a_context(self):
        assert RLSTenantGuard().has_permission(_request_of(_admin())) is False

    def test_a_tenant_the_member_may_select_is_allowed(self, user_a, member_of, tenant_b):
        member_of(user_a, tenant_b)
        with tenant_context(tenant_b.pk):
            assert RLSTenantGuard().has_permission(_request_of(user_a)) is True

    def test_a_tenant_the_user_is_not_a_member_of_is_refused(self, user_a, member_of, tenant_b):
        member_of(user_a)
        with tenant_context(tenant_b.pk):
            assert RLSTenantGuard().has_permission(_request_of(user_a)) is False

    def test_the_user_is_request_auth_before_request_user(self):
        request = _request_of(_scoped(3))
        request.user = _scoped(4)
        with tenant_context(3):
            assert RLSTenantGuard().has_permission(request) is True


@pytest.mark.integration
class TestGuardOnTheApi:
    def test_a_tenant_the_user_selected_passes(
        self, enforce_rls, flavour, user_a, member_of, sample_orders, tenant_b
    ):
        member_of(user_a, tenant_b)

        response = flavour.get("state", user_a, headers={"X-Tenant": str(tenant_b.pk)})

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]

    def test_the_same_tenant_entered_by_other_code_is_refused(
        self, enforce_rls, user_a, member_of, sample_orders, tenant_b
    ):
        member_of(user_a)
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.call("get", f"/api/other-context?tenant={tenant_b.pk}", user=user_a)

        assert response.status_code == 200, response.content
        assert response.json() == {"allowed": False}

    def test_a_tenant_of_the_members_entered_by_other_code_is_allowed(
        self, enforce_rls, user_a, member_of, sample_orders, tenant_b
    ):
        member_of(user_a, tenant_b)
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.call("get", f"/api/other-context?tenant={tenant_b.pk}", user=user_a)

        assert response.json() == {"allowed": True}

    def test_the_tenant_context_an_override_returned_passes(
        self, enforce_rls, twin, user_a, sample_orders, tenant_b
    ):
        response = twin.get("state", user_a, base="/pick", headers={"X-Pick": str(tenant_b.pk)})

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_the_admin_context_is_refused_for_a_non_admin(
        self, enforce_rls, user_a, sample_orders
    ):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.get("state", user_a, base="/admin-for-all")

        assert response.status_code == 403
        assert response.json() == {"detail": RLSTenantGuard.message}
        _assert_context_closed()

    def test_the_admin_context_is_allowed_for_a_tenant_admin(
        self, enforce_rls, api_admin, sample_orders
    ):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.get("state", api_admin, base="/admin-for-all")

        assert response.status_code == 200, response.content
        assert response.json()["is_admin_guc"] == "true"

    def test_an_operation_served_outside_a_context_is_a_403(
        self, enforce_rls, user_a, sample_orders
    ):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        guarded = flavour.get("guarded", user_a, base="/plain")
        unguarded = flavour.get("unguarded", user_a, base="/plain")

        assert guarded.status_code == 403
        assert unguarded.status_code == 200
        assert unguarded.json()["products"] == []  # fail closed without the guard
        _assert_context_closed()

    def test_an_unauthenticated_request_to_a_guarded_operation_is_a_401(
        self, enforce_rls, flavour, sample_orders
    ):
        response = flavour.call("get", "/open/guarded")

        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}


class TestObjectPermission:
    def test_an_object_of_the_active_tenant_is_allowed(self, tenant_a, sample_orders):
        request = _request_of(_scoped(tenant_a.pk))
        with tenant_context(tenant_a.pk):
            assert guard.has_object_permission(request, sample_orders["a1"]) is True

    def test_an_object_of_another_tenant_is_refused(self, tenant_a, sample_orders):
        request = _request_of(_scoped(tenant_a.pk))
        with tenant_context(tenant_a.pk):
            assert guard.has_object_permission(request, sample_orders["b1"]) is False

    def test_any_object_is_allowed_in_the_admin_context(self, sample_orders):
        with admin_context():
            assert guard.has_object_permission(_request_of(_admin()), sample_orders["b1"])

    def test_an_object_without_the_tenant_field_is_allowed(self, tenant_a):
        with tenant_context(tenant_a.pk):
            assert guard.has_object_permission(_request_of(_scoped()), tenant_a) is True
            assert guard.has_object_permission(_request_of(_scoped()), object()) is True

    def test_an_object_outside_a_context_is_refused(self, sample_orders):
        assert guard.has_object_permission(_request_of(_scoped()), sample_orders["a1"]) is False

    @pytest.mark.integration
    def test_the_operation_checks_an_object_of_its_tenant(
        self, enforce_rls, user_a, sample_orders
    ):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.call(
            "get", f"/api/object-permission/{sample_orders['a1'].pk}", user=user_a
        )

        assert response.json() == {"allowed": True}


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


class ByAll(RLSTenantModelSchema):
    class Meta:
        model = Order
        fields = "__all__"


class ByExclude(RLSTenantModelSchema):
    class Meta:
        model = Order
        exclude = ["id"]  # noqa: RUF012  -- Ninja Meta convention


class ByFieldName(RLSTenantModelSchema):
    class Meta:
        model = Order
        fields = ["id", "product", "tenant"]  # noqa: RUF012  -- Ninja Meta convention


class ByFieldAttname(RLSTenantModelSchema):
    class Meta:
        model = Order
        fields = ["id", "product", "tenant_id"]  # noqa: RUF012  -- Ninja Meta convention


class TestRLSTenantModelSchema:
    @pytest.mark.parametrize("schema", [ByAll, ByExclude, ByFieldName, ByFieldAttname])
    def test_the_tenant_field_is_left_out_whatever_meta_says(self, schema):
        assert "tenant" not in schema.model_fields
        assert "tenant_id" not in schema.model_fields
        assert "product" in schema.model_fields

    def test_the_model_may_be_named_by_a_label(self):
        class ByLabel(RLSTenantModelSchema):
            class Meta:
                model = "test_app.Order"
                fields = "__all__"

        assert "tenant" not in ByLabel.model_fields
        assert "product" in ByLabel.model_fields

    def test_the_tenant_field_of_an_external_model_is_left_out(self):
        class BookOut(RLSTenantModelSchema):
            class Meta:
                model = Book
                fields = "__all__"

        assert "owner" not in BookOut.model_fields
        assert "owner_id" not in BookOut.model_fields

    def test_a_model_without_a_tenant_field_is_left_alone(self):
        class TenantOut(RLSTenantModelSchema):
            class Meta:
                model = Tenant
                fields = "__all__"

        assert set(TenantOut.model_fields) == {"id", "name"}

    def test_the_input_schema_has_the_foreign_key_but_not_the_tenant(self):
        assert "order" in ItemIn.model_fields
        assert "tenant" not in ItemIn.model_fields
        assert "tenant_id" not in ItemIn.model_fields

    def test_the_openapi_schema_has_no_tenant_field(self):
        components = api.get_openapi_schema()["components"]["schemas"]

        for name in ("OrderOut", "ItemIn"):
            assert components[name]["properties"], name
            assert "tenant" not in components[name]["properties"]
            assert "tenant_id" not in components[name]["properties"]
        assert "product" in components["OrderOut"]["properties"]

    def test_a_payload_naming_the_tenant_does_not_validate_it(self, tenant_b):
        item = ItemIn.model_validate(
            {"order_id": 1, "description": "d", "tenant": tenant_b.pk, "tenant_id": tenant_b.pk}
        )

        assert "tenant" not in item.model_dump()
        assert "tenant_id" not in item.model_dump()

    @pytest.mark.parametrize("name", ["tenant", "tenant_id"])
    def test_a_declared_tenant_field_is_refused(self, name):
        with pytest.raises(RLSConfigurationError, match=name):

            class Declared(RLSTenantModelSchema):
                __annotations__ = {name: "int | None"}
                locals()[name] = None

                class Meta:
                    model = Order
                    fields = "__all__"

    def test_a_meta_without_fields_is_still_ninjas_error(self):
        with pytest.raises(Exception, match=r"fields|exclude"):

            class Bare(RLSTenantModelSchema):
                class Meta:
                    model = Order


def _item_request(user):
    return _request_of(user)


@pytest.mark.integration
class TestRlsSave:
    def test_the_tenant_comes_from_the_context_not_the_payload(
        self, enforce_rls, user_a, sample_orders, tenant_a, tenant_b
    ):
        payload = {
            "order_id": sample_orders["a1"].pk,
            "description": "x",
            "tenant_id": tenant_b.pk,
            "tenant": tenant_b,
            "id": 987654,
        }
        with tenant_context(tenant_a.pk):
            item = rls_save(_item_request(user_a), OrderItem(), payload)

        assert item.tenant_id == tenant_a.pk
        assert item.pk != 987654
        with admin_context():
            assert OrderItem.objects.get(pk=item.pk).tenant_id == tenant_a.pk

    def test_a_schema_naming_the_tenant_cannot_move_the_row(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a, tenant_b
    ):
        response = Flavour("sync", is_async=False, via_asgi=flavour.via_asgi).post(
            "sneaky-items",
            user_a,
            {
                "description": "x",
                "order_id": sample_orders["a1"].pk,
                "tenant_id": tenant_b.pk,
                "tenant": tenant_b.pk,
            },
        )

        assert response.status_code == 200, response.content
        assert response.json()["tenant_id"] == tenant_a.pk
        _assert_context_closed()

    def test_the_model_schema_writes_in_the_tenant(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a
    ):
        response = flavour.post(
            "items", user_a, {"order_id": sample_orders["a1"].pk, "description": "d"}
        )

        assert response.status_code == 200, response.content
        assert response.json()["tenant_id"] == tenant_a.pk
        with admin_context():
            assert OrderItem.objects.get(pk=response.json()["id"]).tenant_id == tenant_a.pk
        _assert_context_closed()

    def test_exclude_unset_writes_only_the_fields_the_client_sent(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        with admin_context():
            item = OrderItem.objects.create(
                order=sample_orders["a1"], description="old", tenant=tenant_a
            )
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.call(
            "patch", f"/api/items/{item.pk}", user=user_a, body={"description": "new"}
        )

        assert response.status_code == 200, response.content
        assert response.json() == {
            "id": item.pk,
            "description": "new",
            "order_id": sample_orders["a1"].pk,
            "tenant_id": tenant_a.pk,
        }

    def test_without_exclude_unset_unset_fields_are_written(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        with admin_context():
            item = OrderItem.objects.create(
                order=sample_orders["a1"], description="old", tenant=tenant_a
            )
        payload = ninja_views.ItemPatch(description="new")  # order_id is unset: None

        with tenant_context(tenant_a.pk), pytest.raises(IntegrityError), transaction.atomic():
            rls_save(_item_request(user_a), item, payload)

    def test_admin_context_leaves_the_tenant_the_caller_set(
        self, enforce_rls, api_admin, sample_orders, tenant_b
    ):
        with admin_context():
            item = rls_save(
                _item_request(api_admin),
                OrderItem(tenant=tenant_b),
                {"order_id": sample_orders["b1"].pk, "description": "x"},
            )

        assert item.tenant_id == tenant_b.pk

    def test_the_admin_context_does_not_take_the_tenant_from_the_payload(
        self, enforce_rls, api_admin, sample_orders, tenant_a, tenant_b
    ):
        with admin_context():
            item = rls_save(
                _item_request(api_admin),
                OrderItem(tenant=tenant_a),
                {
                    "order_id": sample_orders["a1"].pk,
                    "description": "x",
                    "tenant_id": tenant_b.pk,
                    "tenant": tenant_b.pk,
                },
            )

        assert item.tenant_id == tenant_a.pk

    def test_a_payload_tenant_does_not_replace_the_missing_context(
        self, enforce_rls, user_a, sample_orders, tenant_b
    ):
        with pytest.raises(NoTenantContextError):
            rls_save(
                _item_request(user_a),
                OrderItem(),
                {
                    "order_id": sample_orders["a1"].pk,
                    "description": "x",
                    "tenant_id": tenant_b.pk,
                },
            )

    def test_no_context_and_no_tenant_is_refused(self, enforce_rls, user_a, sample_orders):
        with pytest.raises(NoTenantContextError):
            rls_save(
                _item_request(user_a),
                OrderItem(),
                {"order_id": sample_orders["a1"].pk, "description": "x"},
            )

    def test_a_foreign_key_to_another_tenant_is_a_validation_error(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        with tenant_context(tenant_a.pk), pytest.raises(NinjaValidationError) as caught:
            rls_save(
                _item_request(user_a),
                OrderItem(),
                {"order": sample_orders["b1"].pk, "description": "x"},
            )

        assert [error["loc"] for error in caught.value.errors] == [["body", "payload", "order"]]

    def test_a_missing_target_and_another_tenants_get_the_same_422_body(
        self, enforce_rls, flavour, user_a, sample_orders
    ):
        target = sample_orders["b1"]
        target_pk = target.pk  # delete() clears it
        body = {"order_id": target_pk, "description": "d"}

        other_tenants = flavour.post("items", user_a, body)
        with admin_context():
            target.delete()
        missing = flavour.post("items", user_a, body)

        assert other_tenants.status_code == missing.status_code == 422
        assert other_tenants.content == missing.content
        assert str(target_pk).encode() in missing.content
        _assert_context_closed()

    def test_the_422_does_not_reveal_that_the_row_exists(self, enforce_rls, user_a, sample_orders):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.post(
            "items", user_a, {"order_id": sample_orders["b1"].pk, "description": "d"}
        )

        assert response.status_code == 422
        assert "tenant" not in response.content.decode().lower()


# ---------------------------------------------------------------------------
# Streaming: documented limitation
# ---------------------------------------------------------------------------


async def _consume(path, headers):
    response = await AsyncClient().get(path, headers=headers)
    return response, [chunk async for chunk in response.streaming_content]


@pytest.mark.integration
class TestStreaming:
    """A streaming operation is consumed after ``run()`` returns: it gets no context."""

    def test_a_sync_stream_passes_through_without_a_context(
        self, enforce_rls, user_a, sample_orders, caplog
    ):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        response = Client().get("/stream/stream", headers={"X-User": user_a.username})

        assert response.status_code == 200
        lines = [json.loads(line) for line in b"".join(response.streaming_content).splitlines()]
        assert lines == [{"tenant_id": None, "context_active": False}]
        assert [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME] == []
        _assert_context_closed()

    def test_an_async_stream_passes_through_without_a_context(
        self, enforce_rls, user_a, sample_orders, caplog
    ):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        response, chunks = async_to_sync(_consume)("/stream/astream", {"X-User": user_a.username})

        assert response.status_code == 200
        assert [json.loads(line) for line in b"".join(chunks).splitlines()] == [
            {"tenant_id": None, "context_active": False}
        ]
        assert [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME] == []
        _assert_context_closed()


# ---------------------------------------------------------------------------
# STRICT_MODE and USE_LOCAL_SET
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStrictMode:
    @pytest.fixture(autouse=True)
    def _strict(self):
        with override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "STRICT_MODE": True}):
            yield

    def test_the_request_works(self, enforce_rls, flavour, user_a, sample_orders):
        response = flavour.get("state", user_a)

        assert response.status_code == 200, response.content
        assert response.json()["products"] == ["Widget A1", "Widget A2"]
        _assert_context_closed()

    def test_the_lazy_queryset_is_evaluated_in_the_context(
        self, enforce_rls, flavour, user_a, sample_orders
    ):
        response = flavour.get("lazy", user_a)

        assert response.status_code == 200, response.content
        assert _products(response) == ["Widget A1", "Widget A2"]

    def test_a_selected_tenant(
        self, enforce_rls, flavour, tenantless_user, member_of, sample_orders, tenant_b
    ):
        member_of(tenantless_user, tenant_b)

        response = flavour.get("state", tenantless_user, headers={"X-Tenant": str(tenant_b.pk)})

        assert response.status_code == 200, response.content
        assert response.json()["products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_an_operation_without_a_context_raises(self, enforce_rls, sample_orders):
        response = Client(raise_request_exception=False).get("/open/state")

        assert response.status_code == 500
        _assert_context_closed()

    def test_the_guard_refuses_before_any_query(self, enforce_rls, user_a, sample_orders):
        response = Client().get("/plain/guarded", headers={"X-User": user_a.username})

        assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
class TestLocalSetMode:
    """``USE_LOCAL_SET=True``: the guard applies the context per statement."""

    @pytest.fixture
    def local_set(self):
        """Listed last, so the data fixtures run in session mode."""
        with override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "USE_LOCAL_SET": True}):
            yield

    def test_the_request_sees_its_tenant(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a, local_set
    ):
        response = flavour.get("state", user_a)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] == str(tenant_a.pk)
        _assert_context_closed()

    def test_the_lazy_queryset_is_serialized_in_the_context(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a, local_set
    ):
        response = flavour.get("lazy", user_a)

        assert _products(response) == ["Widget A1", "Widget A2"]
        _assert_context_closed()

    def test_a_selected_tenant(
        self, enforce_rls, flavour, tenantless_user, member_of, sample_orders, tenant_b, local_set
    ):
        member_of(tenantless_user, tenant_b)

        response = flavour.get("state", tenantless_user, headers={"X-Tenant": str(tenant_b.pk)})

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_a_write_lands_in_the_tenant(
        self, enforce_rls, flavour, user_a, sample_orders, tenant_a, local_set
    ):
        response = flavour.post(
            "items", user_a, {"order_id": sample_orders["a1"].pk, "description": "d"}
        )

        assert response.status_code == 200, response.content
        _assert_context_closed()
        with admin_context():
            assert OrderItem.objects.get(pk=response.json()["id"]).tenant_id == tenant_a.pk


# ---------------------------------------------------------------------------
# Audit records (#48)
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_records(caplog):
    """Return a function listing the audit records logged so far."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

    def records():
        return [record for record in caplog.records if record.name == AUDIT_LOGGER_NAME]

    return records


def _denials(records):
    """``(source, reason, tenant_id, admin, user_id)`` of each ``context.denied`` record."""
    return [
        (r.rls_source, r.rls_reason, r.rls_tenant_id, r.rls_admin, r.rls_user_id)
        for r in records
        if r.rls_event == "context.denied"
    ]


def test_ninja_is_an_audit_source():
    assert "ninja" in get_args(audit.Source)


@pytest.mark.integration
class TestAuditRecords:
    """The API's context, the guard and the helper are attributed to ``ninja``."""

    def test_the_request_is_attributed_to_the_user(
        self, audit_records, enforce_rls, flavour, user_a, sample_orders, tenant_a
    ):
        assert flavour.get("state", user_a).status_code == 200

        assert [
            (r.rls_event, r.rls_source, r.rls_tenant_id, r.rls_admin, r.rls_user_id)
            for r in audit_records()
        ] == [
            ("context.entered", "ninja", tenant_a.pk, False, user_a.pk),
            ("context.exited", "ninja", tenant_a.pk, False, user_a.pk),
        ]

    def test_the_admin_context_is_attributed_to_the_admin(
        self, audit_records, enforce_rls, flavour, api_admin, sample_orders
    ):
        assert flavour.get("state", api_admin).status_code == 200

        assert [(r.rls_event, r.rls_source, r.rls_admin) for r in audit_records()] == [
            ("context.entered", "ninja", True),
            ("context.exited", "ninja", True),
        ]

    def test_a_failed_auth_logs_nothing(self, audit_records, enforce_rls, flavour):
        assert flavour.get("state", SimpleNamespace(username="nobody")).status_code == 401
        assert audit_records() == []

    def test_a_tenantless_user_is_denied(
        self, audit_records, enforce_rls, flavour, tenantless_user
    ):
        assert flavour.get("state", tenantless_user).status_code == 403

        assert _denials(audit_records()) == [
            ("ninja", "user_without_tenant", None, None, tenantless_user.pk),
        ]

    def test_a_tenant_that_cannot_be_selected_is_denied(
        self, audit_records, enforce_rls, flavour, user_a, member_of, tenant_b
    ):
        member_of(user_a)

        response = flavour.get("state", user_a, headers={"X-Tenant": str(tenant_b.pk)})

        assert response.status_code == 403
        denied = [r for r in audit_records() if r.rls_event == "context.denied"]
        assert [(r.rls_source, r.rls_reason, r.rls_via) for r in denied] == [
            ("ninja", "tenant_not_selectable", "header"),
        ]

    def test_an_operation_outside_a_context_is_denied_by_the_guard(
        self, audit_records, enforce_rls, user_a
    ):
        response = Client().get("/plain/guarded", headers={"X-User": user_a.username})

        assert response.status_code == 403
        assert _denials(audit_records()) == [("ninja", "no_context", None, None, user_a.pk)]

    def test_an_unauthenticated_request_is_not_logged(self, audit_records, enforce_rls):
        assert Client().get("/open/guarded").status_code == 401
        assert audit_records() == []

    def test_another_context_is_denied(self, audit_records):
        with tenant_context(4):
            assert not RLSTenantGuard().has_permission(_request_of(_scoped(3)))
        with admin_context():
            assert not RLSTenantGuard().has_permission(_request_of(_scoped(3)))

        assert _denials(audit_records()) == [
            ("ninja", "context_mismatch", 4, False, 7),
            ("ninja", "context_mismatch", None, True, 7),
        ]

    def test_the_admin_context_for_a_non_admin_is_denied_on_the_api(
        self, audit_records, enforce_rls, user_a
    ):
        response = Client().get("/admin-for-all/state", headers={"X-User": user_a.username})

        assert response.status_code == 403
        assert _denials(audit_records()) == [
            ("ninja", "context_mismatch", None, True, user_a.pk),
        ]

    def test_an_object_of_another_tenant_is_denied(self, audit_records, sample_orders, tenant_a):
        with tenant_context(tenant_a.pk):
            assert not guard.has_object_permission(
                _request_of(_scoped(tenant_a.pk)), sample_orders["b1"]
            )

        assert [
            (r.rls_source, r.rls_reason, r.rls_model)
            for r in audit_records()
            if r.rls_event == "context.denied"
        ] == [("ninja", "object_of_other_tenant", "test_app.Order")]

    def test_a_target_of_another_tenant_is_logged(
        self, audit_records, enforce_rls, user_a, sample_orders, tenant_a
    ):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.post(
            "items", user_a, {"order_id": sample_orders["b1"].pk, "description": "d"}
        )

        assert response.status_code == 422
        rejected = [r for r in audit_records() if r.rls_event == "fk.cross_tenant_rejected"]
        assert [(r.rls_source, r.rls_user_id) for r in rejected] == [("ninja", user_a.pk)]

    def test_a_missing_target_is_not_logged(
        self, audit_records, enforce_rls, user_a, sample_orders
    ):
        flavour = Flavour("sync", is_async=False, via_asgi=False)

        response = flavour.post("items", user_a, {"order_id": 999999, "description": "d"})

        assert response.status_code == 422
        assert [r for r in audit_records() if r.rls_event == "fk.cross_tenant_rejected"] == []

    def test_rls_save_without_a_tenant_is_denied(
        self, audit_records, enforce_rls, user_a, sample_orders
    ):
        with pytest.raises(NoTenantContextError):
            rls_save(
                _item_request(user_a),
                OrderItem(),
                {"order_id": sample_orders["a1"].pk, "description": "x"},
            )

        assert _denials(audit_records()) == [("ninja", "no_context", None, None, user_a.pk)]


# ---------------------------------------------------------------------------
# ASGI
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAsgi:
    """Under ASGI a ``def`` operation runs in a thread-sensitive hop, ``async def`` in the loop."""

    def test_a_sync_operation_sees_its_tenant_and_leaves_nothing(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        flavour = Flavour("sync-under-asgi", is_async=False, via_asgi=True)

        response = flavour.get("lazy", user_a)

        assert _products(response) == ["Widget A1", "Widget A2"]
        assert all(item["seen_tenant"] == tenant_a.pk for item in response.json())
        _assert_context_closed()

    def test_an_async_operation_sees_its_tenant_and_leaves_nothing(
        self, enforce_rls, user_a, sample_orders, tenant_a
    ):
        flavour = Flavour("async", is_async=True, via_asgi=True)

        response = flavour.get("lazy", user_a)

        assert _products(response) == ["Widget A1", "Widget A2"]
        _assert_context_closed()

    def test_the_fixtures_orders_are_unchanged(self, enforce_rls, user_a, sample_orders):
        flavour = Flavour("async", is_async=True, via_asgi=True)
        flavour.get("state", user_a)

        with admin_context():
            assert Order.objects.count() == 3
