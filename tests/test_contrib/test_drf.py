"""Tests for django_rls_tenants.contrib.drf (issue #43).

DRF authenticates inside the view, after every Django middleware, so a token
client reaches ``RLSTenantMiddleware`` as an anonymous user. The views under test
(``tests/drf_views.py``, routed by ``tests/drf_urls.py``) authenticate with
``TokenAuthentication`` first; the test settings have no ``RLSTenantMiddleware``,
so every context a token request runs in is the mixin's.

The suite splits into:

* unit tests for ``RLSTenantAPIViewMixin.get_rls_context()``, the MRO check,
  ``RLSTenantPermission``, ``CurrentTenantDefault`` and the serializer mixin;
* ``@pytest.mark.integration`` request-cycle tests through ``APIClient`` under
  ``enforce_rls``: row isolation, writes, the context's exit on every path
  (handled error, 500), rendering inside the context, ``STRICT_MODE``, both
  GUC modes, nesting inside the middleware's context, and ASGI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction
from django.test import AsyncClient, override_settings
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework.validators import UniqueTogetherValidator
from rest_framework.views import APIView

from django_rls_tenants.contrib import drf as drf_mod
from django_rls_tenants.contrib.drf import (
    CurrentTenantDefault,
    RLSTenantAPIViewMixin,
    RLSTenantModelSerializer,
    RLSTenantPermission,
)
from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.state import (
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)
from tests.drf_views import InvoiceSerializer, StateView
from tests.session_gucs import session_gucs
from tests.test_app.models import AdminUser, Invoice, Order, Tag, Tenant

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.drf_urls")]

_MIDDLEWARE_WITH_RLS = [
    *settings.MIDDLEWARE,
    "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _api_user(username, *, tenant=None, rls_admin=False):
    """Create an ``AUTH_USER_MODEL`` user and an API token for it."""
    user = AdminUser.objects.create_user(
        username=username,
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant,
        rls_admin=rls_admin,
    )
    Token.objects.create(user=user)
    return user


@pytest.fixture
def user_a(tenant_a):
    """A token user scoped to tenant A."""
    return _api_user("api_a", tenant=tenant_a)


@pytest.fixture
def user_b(tenant_b):
    """A token user scoped to tenant B."""
    return _api_user("api_b", tenant=tenant_b)


@pytest.fixture
def api_admin(db):
    """A token user with cross-tenant RLS admin rights."""
    return _api_user("api_admin", rls_admin=True)


@pytest.fixture
def tenantless_user(db):
    """A token user that is neither a tenant admin nor assigned to a tenant."""
    return _api_user("api_nobody")


@pytest.fixture
def sample_invoices(tenant_a, tenant_b):
    """One invoice per tenant, both numbered ``INV-1``."""
    with admin_context():
        return {
            "a": Invoice.objects.create(tenant=tenant_a, number="INV-1"),
            "b": Invoice.objects.create(tenant=tenant_b, number="INV-1"),
        }


def _token_header(user):
    """The ``Authorization`` header of ``user``'s token, which the middleware cannot see."""
    return {"Authorization": f"Token {user.auth_token.key}"}


def _token_client(user):
    """A client that authenticates with ``user``'s token."""
    return APIClient(headers=_token_header(user))


def _assert_context_closed():
    """Nothing the request entered is left on this thread or its connection."""
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False
    assert _get_context_gucs() is None
    assert session_gucs() == (None, None)


def _order_option(order):
    """The ``<option>`` the browsable API's form renders for ``order``."""
    return f'<option value="{order.pk}"'.encode()


def _scoped(tenant_id=3):
    """An authenticated, tenant-scoped stand-in user."""
    return SimpleNamespace(is_authenticated=True, is_tenant_admin=False, rls_tenant_id=tenant_id)


def _admin():
    """An authenticated tenant-admin stand-in user."""
    return SimpleNamespace(is_authenticated=True, is_tenant_admin=True, rls_tenant_id=None)


def _drf_request(user):
    """A DRF ``Request`` whose ``user`` is already set."""
    return SimpleNamespace(user=user)


# ---------------------------------------------------------------------------
# RLSTenantAPIViewMixin: unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["rest_framework.views", "django_rls_tenants.contrib.drf"])
def test_importable_from_default_permission_classes(first):
    """``tests/settings.py`` lists ``RLSTenantPermission`` in ``DEFAULT_PERMISSION_CLASSES``.

    DRF imports that list while it builds ``APIView``, so ``contrib.drf`` must not
    import ``rest_framework.views`` at module level, whichever is imported first.
    Run in a fresh interpreter: both are already imported here.
    """
    code = (
        "import django; django.setup(); "
        f"import {first}; "
        "from rest_framework.views import APIView; "
        "from django_rls_tenants.contrib.drf import RLSTenantPermission; "
        "assert RLSTenantPermission in APIView.permission_classes"
    )
    result = subprocess.run(  # noqa: S603  -- fixed arguments
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "tests.settings"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class TestMixinMro:
    def test_mixin_after_apiview_is_rejected(self):
        with pytest.raises(RLSConfigurationError, match="List the mixin first"):
            type("Wrong", (APIView, RLSTenantAPIViewMixin), {})

    def test_mixin_first_is_accepted(self):
        view_class = type("Right", (RLSTenantAPIViewMixin, APIView), {})
        assert view_class.__mro__.index(RLSTenantAPIViewMixin) < view_class.__mro__.index(APIView)

    def test_subclass_of_a_correct_view_is_accepted(self):
        type("Sub", (StateView,), {})


class TestGetRlsContext:
    def _context(self, user):
        return StateView().get_rls_context(_drf_request(user))

    def test_anonymous_gets_no_context(self):
        assert self._context(SimpleNamespace(is_authenticated=False)) is None

    def test_missing_user_gets_no_context(self):
        # UNAUTHENTICATED_USER = None makes request.user None.
        assert self._context(None) is None

    def test_scoped_user_gets_their_tenant(self):
        with patch.object(drf_mod, "tenant_context") as tenant_ctx:
            context = self._context(_scoped(7))
        tenant_ctx.assert_called_once_with(7)
        assert context is tenant_ctx.return_value

    def test_admin_gets_admin_context(self):
        with patch.object(drf_mod, "admin_context") as admin_ctx:
            context = self._context(_admin())
        admin_ctx.assert_called_once_with()
        assert context is admin_ctx.return_value

    def test_tenantless_user_is_denied_without_the_hint(self, caplog):
        with pytest.raises(PermissionDenied) as excinfo:
            self._context(_scoped(None))
        assert "rls_tenant_id" not in str(excinfo.value.detail)
        assert "rls_tenant_id" in caplog.text

    def test_matching_active_context_enters_nothing(self):
        with tenant_context(7):
            assert self._context(_scoped(7)) is None
        with admin_context():
            assert self._context(_admin()) is None

    def test_other_active_context_is_replaced(self):
        with tenant_context(8), patch.object(drf_mod, "tenant_context") as tenant_ctx:
            self._context(_scoped(7))
        tenant_ctx.assert_called_once_with(7)

    def test_perform_authentication_outside_dispatch_enters_nothing(self, tenant_a):
        django_request = APIRequestFactory().get("/")
        force_authenticate(django_request, user=_scoped(tenant_a.pk))
        view = StateView()
        request = view.initialize_request(django_request)
        with patch.object(StateView, "get_rls_context") as get_context:
            view.perform_authentication(request)
        get_context.assert_not_called()
        assert get_rls_context_active() is False


# ---------------------------------------------------------------------------
# RLSTenantPermission: unit tests
# ---------------------------------------------------------------------------


class TestRLSTenantPermission:
    def _has_permission(self, user):
        return RLSTenantPermission().has_permission(_drf_request(user), None)

    def _has_object_permission(self, obj):
        return RLSTenantPermission().has_object_permission(_drf_request(_admin()), None, obj)

    def test_anonymous_is_refused(self):
        with tenant_context(3):
            assert not self._has_permission(SimpleNamespace(is_authenticated=False))
            assert not self._has_permission(None)

    def test_no_context_is_refused(self):
        assert not self._has_permission(_scoped(3))
        assert not self._has_permission(_admin())

    def test_scoped_user_in_their_own_context_is_allowed(self):
        with tenant_context(3):
            assert self._has_permission(_scoped(3))

    def test_scoped_user_in_another_tenant_is_refused(self):
        with tenant_context(4):
            assert not self._has_permission(_scoped(3))

    def test_scoped_user_in_the_admin_context_is_refused(self):
        with admin_context():
            assert not self._has_permission(_scoped(3))

    def test_tenantless_user_is_refused(self):
        with tenant_context(3):
            assert not self._has_permission(_scoped(None))

    def test_admin_is_allowed_in_any_context(self):
        with admin_context():
            assert self._has_permission(_admin())
        with tenant_context(3):
            assert self._has_permission(_admin())

    def test_object_of_the_active_tenant_is_allowed(self):
        with tenant_context(3):
            assert self._has_object_permission(Order(tenant_id=3))

    def test_object_of_another_tenant_is_refused(self):
        with tenant_context(3):
            assert not self._has_object_permission(Order(tenant_id=4))
            assert not self._has_object_permission(Order(tenant_id=None))

    def test_any_object_is_allowed_in_the_admin_context(self):
        with admin_context():
            assert self._has_object_permission(Order(tenant_id=4))

    def test_object_without_the_tenant_field_is_allowed(self):
        with tenant_context(3):
            assert self._has_object_permission(Tag(name="shared"))
            assert self._has_object_permission({"not": "a model"})

    def test_object_outside_a_context_is_refused(self):
        assert not self._has_object_permission(Order(tenant_id=3))


# ---------------------------------------------------------------------------
# CurrentTenantDefault and the serializer mixin: unit tests
# ---------------------------------------------------------------------------


class TestCurrentTenantDefault:
    def test_returns_the_active_tenant_without_a_query(self, tenant_a, django_assert_num_queries):
        with tenant_context(tenant_a.pk), django_assert_num_queries(0):
            tenant = CurrentTenantDefault()(None)
        assert isinstance(tenant, Tenant)
        assert tenant.pk == tenant_a.pk

    def test_deferred_fields_load_on_access(self, tenant_a):
        with tenant_context(tenant_a.pk):
            tenant = CurrentTenantDefault()(None)
        assert tenant.name == "Tenant A"

    def test_admin_context_has_no_tenant(self):
        with admin_context(), pytest.raises(NoTenantContextError, match="the admin context"):
            CurrentTenantDefault()(None)

    def test_no_context_has_no_tenant(self):
        with pytest.raises(NoTenantContextError, match="no RLS context"):
            CurrentTenantDefault()(None)

    def test_repr(self):
        assert repr(CurrentTenantDefault()) == "CurrentTenantDefault()"


class AliasingInvoiceSerializer(RLSTenantModelSerializer):
    """Declares fields that would write the tenant FK under other names."""

    tenant_id = serializers.IntegerField()
    owner = serializers.PrimaryKeyRelatedField(source="tenant", queryset=Tenant.objects.all())
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = Invoice
        fields = ("id", "number", "tenant_id", "owner", "tenant_name")


class TestRLSTenantSerializerMixin:
    def test_tenant_context_hides_the_tenant_field(self, tenant_a):
        with tenant_context(tenant_a.pk):
            field = InvoiceSerializer().fields["tenant"]
        assert isinstance(field, serializers.HiddenField)
        assert isinstance(field.default, CurrentTenantDefault)

    def test_tenant_field_is_added_when_not_declared(self, tenant_a):
        class NumberOnly(RLSTenantModelSerializer):
            class Meta:
                model = Invoice
                fields = ("id", "number")

        with tenant_context(tenant_a.pk):
            fields = NumberOnly().fields
        assert isinstance(fields["tenant"], serializers.HiddenField)

    def test_no_context_hides_the_tenant_field(self):
        assert isinstance(InvoiceSerializer().fields["tenant"], serializers.HiddenField)

    def test_admin_context_keeps_the_declared_field(self):
        with admin_context():
            field = InvoiceSerializer().fields["tenant"]
        assert isinstance(field, serializers.PrimaryKeyRelatedField)

    def test_model_without_the_tenant_field_is_left_alone(self, tenant_a):
        class TagSerializer(RLSTenantModelSerializer):
            class Meta:
                model = Tag
                fields = ("id", "name")

        with tenant_context(tenant_a.pk):
            assert list(TagSerializer().fields) == ["id", "name"]

    def test_other_fields_writing_the_tenant_are_removed(self, tenant_a, tenant_b):
        with tenant_context(tenant_a.pk):
            fields = AliasingInvoiceSerializer().fields
        assert "tenant_id" not in fields
        assert "owner" not in fields
        assert "tenant_name" in fields  # read-only
        assert isinstance(fields["tenant"], serializers.HiddenField)

    def test_admin_context_keeps_fields_writing_the_tenant(self):
        with admin_context():
            fields = AliasingInvoiceSerializer().fields
        assert {"tenant_id", "owner", "tenant_name"} <= set(fields)

    def test_aliases_cannot_write_another_tenant(self, enforce_rls, tenant_a, tenant_b):
        payload = {"number": "INV-8", "tenant_id": tenant_b.pk, "owner": tenant_b.pk}
        with tenant_context(tenant_a.pk):
            serializer = AliasingInvoiceSerializer(data=payload)
            assert serializer.is_valid(), serializer.errors
            invoice = serializer.save()
        assert invoice.tenant_id == tenant_a.pk

    def test_unique_constraint_with_the_tenant_is_still_validated(self, tenant_a):
        with tenant_context(tenant_a.pk):
            validators = InvoiceSerializer().get_validators()
        assert any(
            isinstance(v, UniqueTogetherValidator) and set(v.fields) == {"tenant", "number"}
            for v in validators
        )

    def test_create_sets_the_tenant_from_the_context(self, enforce_rls, tenant_a):
        with tenant_context(tenant_a.pk):
            serializer = InvoiceSerializer(data={"number": "INV-9"})
            assert serializer.is_valid(), serializer.errors
            invoice = serializer.save()
            assert invoice.tenant_id == tenant_a.pk
            assert "tenant" not in serializer.data


# ---------------------------------------------------------------------------
# Request cycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTokenRequests:
    """Token clients: the mixin alone sets the context (no middleware)."""

    def test_scoped_user_sees_only_their_rows(self, enforce_rls, user_a, sample_orders, tenant_a):
        response = _token_client(user_a).get("/api/state/")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_id"] == tenant_a.pk
        assert body["tenant_guc"] == str(tenant_a.pk)
        assert body["is_admin_guc"] == "false"
        _assert_context_closed()

    def test_admin_sees_every_tenant(self, enforce_rls, api_admin, sample_orders):
        response = _token_client(api_admin).get("/api/state/")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["database_products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        assert body["tenant_id"] is None
        assert body["context_active"] is True
        assert body["is_admin_guc"] == "true"
        _assert_context_closed()

    def test_anonymous_request_is_refused(self, enforce_rls, sample_orders):
        response = APIClient().get("/api/state/")

        assert response.status_code == 401
        _assert_context_closed()

    def test_tenantless_user_gets_403(self, enforce_rls, tenantless_user):
        response = _token_client(tenantless_user).get("/api/state/")

        assert response.status_code == 403
        assert response.json() == {"detail": "Your account is not assigned to a tenant."}
        _assert_context_closed()

    def test_unmixed_view_is_refused_by_the_permission(self, enforce_rls, user_a, sample_orders):
        response = _token_client(user_a).get("/api/unmixed/")

        assert response.status_code == 403
        _assert_context_closed()

    def test_admin_override_picks_a_tenant(self, enforce_rls, api_admin, sample_orders, tenant_b):
        client = _token_client(api_admin)

        response = client.get("/api/tenant-header/", headers={"X-Tenant": str(tenant_b.pk)})

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_lazy_response_data_is_rendered_inside_the_context(
        self, enforce_rls, user_a, sample_orders
    ):
        response = _token_client(user_a).get("/api/lazy/")

        assert response.status_code == 200, response.content
        assert response.json() == ["Widget A1", "Widget A2"]
        _assert_context_closed()

    def test_browsable_api_form_is_rendered_inside_the_context(
        self, enforce_rls, user_a, sample_orders
    ):
        response = _token_client(user_a).get("/api/order-items/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        # The form's ``order`` dropdown lists tenant A's orders, and only those.
        assert _order_option(sample_orders["a1"]) in response.content
        assert _order_option(sample_orders["b1"]) not in response.content
        _assert_context_closed()


@pytest.mark.integration
class TestContextExit:
    """The context ends with the request on every path, and the next request starts clean."""

    def test_handled_error_closes_the_context(self, enforce_rls, user_a, sample_orders):
        response = _token_client(user_a).get("/api/invalid/")

        assert response.status_code == 400
        _assert_context_closed()

    def test_unhandled_error_closes_the_context(self, enforce_rls, user_a, sample_orders):
        client = _token_client(user_a)
        client.raise_request_exception = False

        response = client.get("/api/boom/")

        assert response.status_code == 500
        _assert_context_closed()

        # The next request on this thread (and connection) is anonymous: no rows.
        anonymous = APIClient().get("/api/unmixed/")
        assert anonymous.status_code == 401
        assert session_gucs() == (None, None)

    def test_exception_propagating_out_of_dispatch_closes_the_context(
        self, enforce_rls, user_a, sample_orders
    ):
        with pytest.raises(RuntimeError, match="boom"):
            _token_client(user_a).get("/api/boom/")
        _assert_context_closed()

    def test_outer_context_is_restored(self, enforce_rls, user_a, sample_orders, tenant_b):
        with tenant_context(tenant_b.pk):
            response = _token_client(user_a).get("/api/state/")
            assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
            assert get_current_tenant_id() == tenant_b.pk
            assert session_gucs() == (str(tenant_b.pk), "false")
        _assert_context_closed()


@pytest.mark.integration
class TestWrites:
    def test_create_takes_the_tenant_from_the_context(
        self, enforce_rls, user_a, tenant_a, tenant_b
    ):
        response = _token_client(user_a).post(
            "/api/invoices/", {"number": "INV-2", "tenant": tenant_b.pk}, format="json"
        )

        assert response.status_code == 201, response.content
        assert response.json() == {"id": response.json()["id"], "number": "INV-2"}
        with admin_context():
            invoice = Invoice.objects.get(number="INV-2")
        assert invoice.tenant_id == tenant_a.pk

    def test_duplicate_number_in_the_same_tenant_is_a_400(
        self, enforce_rls, user_a, sample_invoices
    ):
        response = _token_client(user_a).post("/api/invoices/", {"number": "INV-1"}, format="json")

        assert response.status_code == 400
        assert "non_field_errors" in response.json()

    def test_other_tenants_object_is_not_found(self, enforce_rls, user_a, sample_invoices):
        other = sample_invoices["b"]

        response = _token_client(user_a).patch(
            f"/api/invoices/{other.pk}/", {"number": "STOLEN"}, format="json"
        )

        assert response.status_code == 404
        with admin_context():
            assert Invoice.objects.get(pk=other.pk).number == "INV-1"

    def test_update_cannot_move_an_object_to_another_tenant(
        self, enforce_rls, user_a, sample_invoices, tenant_a, tenant_b
    ):
        own = sample_invoices["a"]

        response = _token_client(user_a).put(
            f"/api/invoices/{own.pk}/", {"number": "INV-5", "tenant": tenant_b.pk}, format="json"
        )

        assert response.status_code == 200, response.content
        with admin_context():
            assert Invoice.objects.get(pk=own.pk).tenant_id == tenant_a.pk

    def test_admin_chooses_the_tenant(self, enforce_rls, api_admin, tenant_b):
        response = _token_client(api_admin).post(
            "/api/invoices/", {"number": "INV-7", "tenant": tenant_b.pk}, format="json"
        )

        assert response.status_code == 201, response.content
        assert response.json()["tenant"] == tenant_b.pk
        with admin_context():
            assert Invoice.objects.get(number="INV-7").tenant_id == tenant_b.pk


@pytest.mark.integration
class TestStrictMode:
    @pytest.fixture(autouse=True)
    def _strict(self):
        with override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "STRICT_MODE": True}):
            yield

    def test_token_request_works(self, enforce_rls, user_a, sample_orders):
        response = _token_client(user_a).get("/api/state/")

        assert response.status_code == 200, response.content
        assert response.json()["products"] == ["Widget A1", "Widget A2"]

    def test_unmixed_view_is_refused_before_any_query(self, enforce_rls, user_a, sample_orders):
        response = _token_client(user_a).get("/api/unmixed/")

        assert response.status_code == 403

    def test_without_the_permission_an_unmixed_view_raises(self, enforce_rls, user_a):
        class Unprotected(APIView):
            permission_classes = (IsAuthenticated,)

            def get(self, request):
                return list(Order.objects.all())

        request = APIRequestFactory().get("/")
        force_authenticate(request, user=user_a)
        with pytest.raises(NoTenantContextError):
            Unprotected.as_view()(request)


@pytest.fixture(params=[False, True], ids=["no-atomic-requests", "atomic-requests"])
def atomic_requests(request):
    """Run a test with and without Django's ``ATOMIC_REQUESTS``."""
    from django.db import connection  # noqa: PLC0415

    previous = connection.settings_dict.get("ATOMIC_REQUESTS", False)
    connection.settings_dict["ATOMIC_REQUESTS"] = request.param
    try:
        yield request.param
    finally:
        connection.settings_dict["ATOMIC_REQUESTS"] = previous


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
class TestLocalSetMode:
    """``USE_LOCAL_SET=True``: SET LOCAL inside ATOMIC_REQUESTS, the guard outside it."""

    @pytest.fixture
    def local_set(self):
        """Listed last, so the data fixtures run in session mode."""
        with override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "USE_LOCAL_SET": True}):
            yield

    def test_token_request_sees_its_tenant(
        self, enforce_rls, atomic_requests, user_a, sample_orders, tenant_a, local_set
    ):
        response = _token_client(user_a).get("/api/state/")

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] == str(tenant_a.pk)
        _assert_context_closed()

    def test_write_lands_in_the_tenant(
        self, enforce_rls, atomic_requests, user_a, tenant_a, local_set
    ):
        response = _token_client(user_a).post("/api/invoices/", {"number": "INV-3"}, format="json")

        assert response.status_code == 201, response.content
        _assert_context_closed()
        with override_settings(RLS_TENANTS={**settings.RLS_TENANTS}), admin_context():
            assert Invoice.objects.get(number="INV-3").tenant_id == tenant_a.pk


@pytest.mark.integration
class TestAtomicRequests:
    """A handled error marks the ATOMIC_REQUESTS transaction for rollback."""

    def test_handled_error_on_a_browsable_page_is_not_a_500(
        self, enforce_rls, atomic_requests, user_a, sample_orders
    ):
        response = _token_client(user_a).post(
            "/api/order-items/", {"description": ""}, headers={"Accept": "text/html"}
        )

        assert response.status_code == 400
        _assert_context_closed()

    def test_successful_browsable_page_is_still_rendered_in_the_context(
        self, enforce_rls, atomic_requests, user_a, sample_orders
    ):
        response = _token_client(user_a).get("/api/order-items/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert _order_option(sample_orders["a1"]) in response.content
        assert _order_option(sample_orders["b1"]) not in response.content
        _assert_context_closed()

    def test_rollback_pending(self):
        assert drf_mod._rollback_pending() is False
        with transaction.atomic():
            assert drf_mod._rollback_pending() is False
            transaction.set_rollback(True)
            assert drf_mod._rollback_pending() is True
            transaction.set_rollback(False)


@pytest.mark.integration
class TestWithMiddleware:
    """``RLSTenantMiddleware`` in front: session users and mixed authentication."""

    @pytest.fixture
    def rls_middleware(self):
        """Listed last, so the data fixtures run before the middleware is swapped in."""
        with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS):
            yield

    def test_session_user_reuses_the_middleware_context(
        self, enforce_rls, user_a, sample_orders, rls_middleware
    ):
        client = APIClient()
        client.force_login(user_a)

        with (
            patch.object(drf_mod, "tenant_context", wraps=tenant_context) as tenant_ctx,
            patch.object(drf_mod, "admin_context", wraps=admin_context) as admin_ctx,
        ):
            response = client.get("/api/state/")

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        tenant_ctx.assert_not_called()
        admin_ctx.assert_not_called()
        _assert_context_closed()

    def test_token_user_replaces_the_session_users_context(
        self, enforce_rls, user_a, user_b, sample_orders, rls_middleware
    ):
        # Logged in as A, but the token (authenticated first) is B's.
        client = _token_client(user_b)
        client.force_login(user_a)

        response = client.get("/api/state/")

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Gadget B1"]
        _assert_context_closed()

    def test_session_user_browsable_api(self, enforce_rls, user_a, sample_orders, rls_middleware):
        client = APIClient()
        client.force_login(user_a)

        response = client.get("/api/order-items/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert _order_option(sample_orders["a1"]) in response.content
        assert _order_option(sample_orders["b1"]) not in response.content
        _assert_context_closed()

    def test_request_drf_left_anonymous_enters_nothing(
        self, enforce_rls, user_a, sample_orders, rls_middleware
    ):
        """The view keeps the middleware's context; ``RLSTenantPermission`` refuses it."""
        client = APIClient()
        client.force_login(user_a)

        with patch.object(drf_mod, "tenant_context", wraps=tenant_context) as tenant_ctx:
            open_view = client.get("/api/token-only/open/")
            protected = client.get("/api/token-only/protected/")

        tenant_ctx.assert_not_called()
        assert open_view.status_code == 200
        assert open_view.json()["user_authenticated"] is False
        assert open_view.json()["context_active"] is True
        assert protected.status_code == 401
        _assert_context_closed()


@pytest.mark.integration
class TestAsgi:
    """Under ASGI Django runs the (synchronous) DRF view in one thread-sensitive hop."""

    def test_token_request_and_the_next_request(self, enforce_rls, user_a, sample_orders):
        response = async_to_sync(AsyncClient().get)("/api/state/", headers=_token_header(user_a))

        assert response.status_code == 200, response.content
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        _assert_context_closed()

        # The hop ran on this thread: its next request starts without a context.
        anonymous = async_to_sync(AsyncClient().get)("/api/unmixed/")
        assert anonymous.status_code == 401
        _assert_context_closed()

    def test_unhandled_error_closes_the_context(self, enforce_rls, user_a, sample_orders):
        client = AsyncClient(raise_request_exception=False)

        response = async_to_sync(client.get)("/api/boom/", headers=_token_header(user_a))

        assert response.status_code == 500
        _assert_context_closed()
