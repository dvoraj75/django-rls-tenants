"""Tests for RLSTenantModelAdmin (issue #31).

Two layers:

* **Unit** -- ``RequestFactory`` requests with lightweight stand-in users drive the
  pure decision methods (``_effective_tenant_id``, ``_rls_context``,
  ``get_exclude``, ``get_list_filter``, ``_sync_tenant_selection``) and the
  switcher filter, with no row access (a few touch the DB only to read tenants).
* **Integration** (``@pytest.mark.integration`` + ``enforce_rls``) -- the full
  admin driven through ``django.test.Client`` under live RLS, proving changelist
  scoping, the tenant switcher, FK hiding + auto-assignment, the fail-closed 403,
  cross-tenant blocking, and autocomplete scoping.

The admins under test are registered in ``tests/test_app/admin.py``; the swapped-in
``AdminUser`` model satisfies the ``TenantUser`` protocol (``is_tenant_admin`` /
``rls_tenant_id``). Test users are Django superusers (so they clear the admin's
own permission checks without per-model grants) while ``rls_admin`` / ``tenant``
independently drive the RLS role the mixin reads.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, ClassVar

import django
import pytest
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, override_settings

from django_rls_tenants.tenants import admin as admin_mod
from django_rls_tenants.tenants.admin import (
    _ALL_TENANTS,
    RLSTenantModelAdmin,
    TenantSwitchListFilter,
)
from django_rls_tenants.tenants.context import admin_context
from django_rls_tenants.tenants.errors import HINT_USER_NO_TENANT
from tests.test_app.models import AdminUser, Order

# ---------------------------------------------------------------------------
# Unit-test helpers
# ---------------------------------------------------------------------------


def _order_admin(**attrs: Any) -> RLSTenantModelAdmin:
    """Build an RLSTenantModelAdmin for Order, overriding class attrs if given."""
    cls = type("Adm", (RLSTenantModelAdmin,), attrs) if attrs else RLSTenantModelAdmin
    return cls(Order, admin.site)


def _request(
    user: Any, *, session: dict[str, Any] | None = None, get: dict[str, Any] | None = None
):
    """A RequestFactory GET with a user and a plain-dict session attached."""
    request = RequestFactory().get("/admin/test_app/order/", get or {})
    request.user = user
    request.session = {} if session is None else session  # type: ignore[attr-defined]
    return request


def _admin_user() -> SimpleNamespace:
    """A cross-tenant admin stand-in (is_tenant_admin=True)."""
    return SimpleNamespace(is_tenant_admin=True, rls_tenant_id=None)


def _scoped_user(tenant_id: int | None = 3) -> SimpleNamespace:
    """A tenant-scoped, non-admin stand-in."""
    return SimpleNamespace(is_tenant_admin=False, rls_tenant_id=tenant_id)


def _filter_params(**params: str) -> dict[str, Any]:
    """Build changelist filter params the way the running Django's ChangeList does.

    Django < 5.0 hands list filters scalar query values; >= 5.0 hands them
    single-item lists (``dict(request.GET.lists())``) and ``SimpleListFilter``
    reads ``value[-1]``. Mirroring that keeps the switcher unit tests correct on
    the whole 4.2--6.0 support matrix -- a bare ``["77"]`` returns ``["77"]`` (not
    ``"77"``) from ``value()`` on 4.2.
    """
    if django.VERSION >= (5, 0):
        return {key: [value] for key, value in params.items()}
    return dict(params)


# ---------------------------------------------------------------------------
# _effective_tenant_id
# ---------------------------------------------------------------------------


class TestEffectiveTenantId:
    """The effective tenant comes from the switcher (admins) or the user."""

    def test_admin_without_selection_is_none(self):
        assert _order_admin()._effective_tenant_id(_request(_admin_user())) is None

    def test_admin_uses_session_selection(self):
        request = _request(_admin_user(), session={"rls_admin_tenant": "9"})
        assert _order_admin()._effective_tenant_id(request) == "9"

    def test_admin_switch_disabled_ignores_session(self):
        request = _request(_admin_user(), session={"rls_admin_tenant": "9"})
        adm = _order_admin(rls_allow_tenant_switch=False)
        assert adm._effective_tenant_id(request) is None

    def test_scoped_user_returns_own_tenant(self):
        assert _order_admin()._effective_tenant_id(_request(_scoped_user(7))) == 7


# ---------------------------------------------------------------------------
# _rls_context
# ---------------------------------------------------------------------------


class TestRlsContext:
    """_rls_context picks tenant/admin context or fails closed."""

    @pytest.fixture(autouse=True)
    def _patch_contexts(self, monkeypatch):
        """Replace the context managers with sentinels to test branch selection."""
        self.calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            admin_mod, "tenant_context", lambda tid: self.calls.append(("tenant", tid)) or "TC"
        )
        monkeypatch.setattr(
            admin_mod, "admin_context", lambda: self.calls.append(("admin",)) or "AC"
        )

    def test_scoped_user_uses_tenant_context(self):
        result = _order_admin()._rls_context(_request(_scoped_user(7)))
        assert result == "TC"
        assert self.calls == [("tenant", 7)]

    def test_admin_without_selection_uses_admin_context(self):
        result = _order_admin()._rls_context(_request(_admin_user()))
        assert result == "AC"
        assert self.calls == [("admin",)]

    def test_admin_with_selection_uses_tenant_context(self):
        request = _request(_admin_user(), session={"rls_admin_tenant": "4"})
        result = _order_admin()._rls_context(request)
        assert result == "TC"
        assert self.calls == [("tenant", "4")]

    def test_scoped_user_without_tenant_denies(self):
        with pytest.raises(PermissionDenied) as exc_info:
            _order_admin()._rls_context(_request(_scoped_user(None)))
        assert HINT_USER_NO_TENANT in str(exc_info.value)

    def test_deny_disabled_returns_nullcontext(self):
        adm = _order_admin(rls_deny_without_tenant=False)
        result = adm._rls_context(_request(_scoped_user(None)))
        assert isinstance(result, nullcontext)


# ---------------------------------------------------------------------------
# get_exclude
# ---------------------------------------------------------------------------


class TestGetExclude:
    """The tenant FK is hidden only when the effective tenant is implicit."""

    def test_hidden_for_scoped_user(self):
        result = _order_admin().get_exclude(_request(_scoped_user(7)))
        assert result is not None
        assert "tenant" in result

    def test_hidden_for_admin_with_selection(self):
        request = _request(_admin_user(), session={"rls_admin_tenant": "4"})
        result = _order_admin().get_exclude(request)
        assert result is not None
        assert "tenant" in result

    def test_visible_for_admin_without_selection(self):
        result = _order_admin().get_exclude(_request(_admin_user()))
        assert not result or "tenant" not in result

    def test_preserves_existing_exclude(self):
        adm = _order_admin(exclude=("amount",))
        result = adm.get_exclude(_request(_scoped_user(7)))
        assert result is not None
        assert "amount" in result
        assert "tenant" in result

    def test_does_not_duplicate_already_excluded_field(self):
        adm = _order_admin(exclude=("tenant",))
        result = adm.get_exclude(_request(_scoped_user(7)))
        assert result is not None
        assert list(result).count("tenant") == 1


# ---------------------------------------------------------------------------
# get_fieldsets
# ---------------------------------------------------------------------------


class TestGetFieldsets:
    """Explicit fieldsets drop the tenant FK exactly when get_exclude hides it.

    Without this, a layout that still names the (now-excluded) field makes Django
    raise ``KeyError`` while rendering the form for scoped users.
    """

    _FIELDSETS: ClassVar[Any] = [(None, {"fields": ["product", "amount", "tenant"]})]

    @staticmethod
    def _flatten(fieldsets: Any) -> list[Any]:
        names: list[Any] = []
        for _name, opts in fieldsets:
            for entry in opts["fields"]:
                names.extend(entry if isinstance(entry, (list, tuple)) else [entry])
        return names

    def test_strips_tenant_for_scoped_user(self):
        adm = _order_admin(fieldsets=self._FIELDSETS)
        result = self._flatten(adm.get_fieldsets(_request(_scoped_user(7))))
        assert "tenant" not in result
        assert "product" in result

    def test_strips_tenant_for_admin_with_selection(self):
        adm = _order_admin(fieldsets=self._FIELDSETS)
        request = _request(_admin_user(), session={"rls_admin_tenant": "4"})
        assert "tenant" not in self._flatten(adm.get_fieldsets(request))

    def test_keeps_tenant_for_admin_without_selection(self):
        adm = _order_admin(fieldsets=self._FIELDSETS)
        assert "tenant" in self._flatten(adm.get_fieldsets(_request(_admin_user())))

    def test_strips_tenant_from_grouped_line(self):
        adm = _order_admin(fieldsets=[(None, {"fields": ["product", ("amount", "tenant")]})])
        result = self._flatten(adm.get_fieldsets(_request(_scoped_user(7))))
        assert "tenant" not in result
        assert "amount" in result  # the rest of the group survives

    def test_drops_group_left_empty(self):
        adm = _order_admin(fieldsets=[(None, {"fields": ["product", ("tenant",)]})])
        result = adm.get_fieldsets(_request(_scoped_user(7)))
        # The ("tenant",) group becomes empty and is dropped entirely, not kept as ().
        assert result[0][1]["fields"] == ["product"]

    def test_does_not_mutate_class_fieldsets(self):
        adm = _order_admin(fieldsets=self._FIELDSETS)
        adm.get_fieldsets(_request(_scoped_user(7)))
        # The admin's own fieldsets must be intact for the next request/user.
        assert "tenant" in self._flatten(adm.fieldsets)


# ---------------------------------------------------------------------------
# get_list_filter
# ---------------------------------------------------------------------------


class TestGetListFilter:
    """The switcher is prepended only for switch-capable admins."""

    def test_admin_gets_switcher(self):
        filters = _order_admin().get_list_filter(_request(_admin_user()))
        assert filters
        assert issubclass(filters[0], TenantSwitchListFilter)

    def test_scoped_user_has_no_switcher(self):
        filters = _order_admin().get_list_filter(_request(_scoped_user(7)))
        assert not any(
            isinstance(f, type) and issubclass(f, TenantSwitchListFilter) for f in filters
        )

    def test_switch_disabled_has_no_switcher(self):
        adm = _order_admin(rls_allow_tenant_switch=False)
        filters = adm.get_list_filter(_request(_admin_user()))
        assert not filters

    def test_custom_query_param_binds_filter(self):
        adm = _order_admin(rls_tenant_query_param="org")
        filters = adm.get_list_filter(_request(_admin_user()))
        assert issubclass(filters[0], TenantSwitchListFilter)
        assert filters[0].parameter_name == "org"


# ---------------------------------------------------------------------------
# _sync_tenant_selection (no DB)
# ---------------------------------------------------------------------------


class TestSyncTenantSelectionNoDb:
    """Selection sync paths that need no database."""

    def test_non_switch_user_is_noop(self):
        request = _request(
            _scoped_user(7), session={"rls_admin_tenant": "1"}, get={"rls_tenant": "2"}
        )
        _order_admin()._sync_tenant_selection(request)
        assert request.session == {"rls_admin_tenant": "1"}

    def test_all_clears_selection(self):
        request = _request(
            _admin_user(), session={"rls_admin_tenant": "1"}, get={"rls_tenant": _ALL_TENANTS}
        )
        _order_admin()._sync_tenant_selection(request)
        assert "rls_admin_tenant" not in request.session

    def test_absent_param_keeps_selection(self):
        request = _request(_admin_user(), session={"rls_admin_tenant": "1"})
        _order_admin()._sync_tenant_selection(request)
        assert request.session["rls_admin_tenant"] == "1"


# ---------------------------------------------------------------------------
# _sync_tenant_selection + switcher filter (DB, no RLS enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSyncTenantSelectionDb:
    """Selection sync paths that validate the id against real tenants."""

    def test_valid_tenant_is_stored(self, tenant_a):
        request = _request(_admin_user(), get={"rls_tenant": str(tenant_a.pk)})
        _order_admin()._sync_tenant_selection(request)
        assert request.session["rls_admin_tenant"] == str(tenant_a.pk)

    def test_unknown_tenant_is_cleared(self, tenant_a):
        request = _request(
            _admin_user(), session={"rls_admin_tenant": "1"}, get={"rls_tenant": "999999"}
        )
        _order_admin()._sync_tenant_selection(request)
        assert "rls_admin_tenant" not in request.session

    def test_non_castable_tenant_is_cleared(self, tenant_a):
        request = _request(
            _admin_user(), session={"rls_admin_tenant": "1"}, get={"rls_tenant": "abc"}
        )
        _order_admin()._sync_tenant_selection(request)
        assert "rls_admin_tenant" not in request.session


@pytest.mark.django_db
class TestSwitcherFilter:
    """TenantSwitchListFilter lists tenants and reflects the persisted choice."""

    def test_lookups_list_all_tenants(self, tenant_a, tenant_b):
        flt = TenantSwitchListFilter(_request(_admin_user()), {}, Order, _order_admin())
        labels = dict(flt.lookup_choices)
        assert labels[str(tenant_a.pk)] == "Tenant A"
        assert labels[str(tenant_b.pk)] == "Tenant B"

    def test_value_falls_back_to_session(self, tenant_a):
        request = _request(_admin_user(), session={"rls_admin_tenant": "55"})
        flt = TenantSwitchListFilter(request, {}, Order, _order_admin())
        assert flt.value() == "55"

    def test_query_param_beats_session(self, tenant_a):
        request = _request(_admin_user(), session={"rls_admin_tenant": "55"})
        flt = TenantSwitchListFilter(
            request, _filter_params(rls_tenant="77"), Order, _order_admin()
        )
        assert flt.value() == "77"

    def test_all_choice_uses_sentinel(self, tenant_a):
        flt = TenantSwitchListFilter(_request(_admin_user()), {}, Order, _order_admin())

        class _ChangeList:
            def get_query_string(self, new_params=None, remove=None):
                return "?" + "&".join(f"{k}={v}" for k, v in (new_params or {}).items())

        choices = list(flt.choices(_ChangeList()))
        assert choices[0]["query_string"] == f"?rls_tenant={_ALL_TENANTS}"
        assert choices[0]["selected"] is True  # nothing selected -> "All"


# ---------------------------------------------------------------------------
# Integration: the live admin under RLS enforcement
# ---------------------------------------------------------------------------


@pytest.fixture
def superuser_admin(db):
    """Cross-tenant admin: Django superuser + is_tenant_admin (rls_admin=True)."""
    return AdminUser.objects.create_superuser(username="root", password="pw", rls_admin=True)  # noqa: S106


@pytest.fixture
def scoped_admin_a(db, tenant_a):
    """Tenant-scoped staff user: Django superuser but RLS-scoped to tenant A."""
    return AdminUser.objects.create_superuser(
        username="staff_a",
        password="pw",  # noqa: S106
        rls_admin=False,
        tenant=tenant_a,
    )


@pytest.fixture
def orphan_user(db):
    """Non-admin user with no tenant -- the fail-closed 403 case."""
    return AdminUser.objects.create_superuser(
        username="orphan",
        password="pw",  # noqa: S106
        rls_admin=False,
        tenant=None,
    )


_AUTOCOMPLETE_ORDER = {
    "app_label": "test_app",
    "model_name": "orderitem",
    "field_name": "order",
    "term": "",
}

# The base test harness omits RLSTenantMiddleware (so the mixin's own fail-closed
# 403 path is reachable). Tests that exercise site-level admin views -- the
# Django >= 6.0 autocomplete endpoint, which the mixin cannot wrap -- opt the
# middleware back in, since that is what sets the per-request RLS context for
# requests not routed through a wrapped ModelAdmin view.
_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
]


@pytest.mark.integration
@pytest.mark.django_db
class TestSuperuserAdminChangelist:
    """A cross-tenant admin sees everything and can switch tenants."""

    def test_sees_all_tenants(self, enforce_rls, client, superuser_admin, sample_orders):
        client.force_login(superuser_admin)
        body = client.get("/admin/test_app/order/").content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" in body

    def test_switcher_scopes_changelist(
        self, enforce_rls, client, superuser_admin, sample_orders, tenant_a
    ):
        client.force_login(superuser_admin)
        body = client.get(
            "/admin/test_app/order/", {"rls_tenant": str(tenant_a.pk)}
        ).content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" not in body

    def test_switcher_selection_persists_and_assigns(
        self, enforce_rls, client, superuser_admin, sample_orders, tenant_a
    ):
        client.force_login(superuser_admin)
        # Select tenant A via the switcher...
        client.get("/admin/test_app/order/", {"rls_tenant": str(tenant_a.pk)})
        # ...then add with no query param: the selection persists and is assigned.
        response = client.post(
            "/admin/test_app/order/add/",
            {"product": "Switched", "amount": "9.00", "_save": "Save"},
        )
        assert response.status_code == 302
        with admin_context():
            assert Order.objects.get(product="Switched").tenant_id == tenant_a.pk

    def test_all_clears_back_to_every_tenant(
        self, enforce_rls, client, superuser_admin, sample_orders, tenant_a
    ):
        client.force_login(superuser_admin)
        client.get("/admin/test_app/order/", {"rls_tenant": str(tenant_a.pk)})
        body = client.get("/admin/test_app/order/", {"rls_tenant": _ALL_TENANTS}).content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" in body

    def test_switcher_is_rendered(
        self, enforce_rls, client, superuser_admin, sample_orders, tenant_a, tenant_b
    ):
        client.force_login(superuser_admin)
        body = client.get("/admin/test_app/order/").content.decode()
        assert "Tenant A" in body
        assert "Tenant B" in body

    def test_add_form_shows_tenant_fk(self, enforce_rls, client, superuser_admin, tenant_a):
        client.force_login(superuser_admin)
        body = client.get("/admin/test_app/order/add/").content.decode()
        assert 'name="tenant"' in body

    def test_add_without_selection_uses_submitted_tenant(
        self, enforce_rls, client, superuser_admin, tenant_b
    ):
        """With no selection (admin_context) the admin's explicit tenant choice stands."""
        client.force_login(superuser_admin)
        response = client.post(
            "/admin/test_app/order/add/",
            {
                "product": "Explicit B",
                "amount": "1.00",
                "tenant": str(tenant_b.pk),
                "_save": "Save",
            },
        )
        assert response.status_code == 302
        with admin_context():
            assert Order.objects.get(product="Explicit B").tenant_id == tenant_b.pk


@pytest.mark.integration
@pytest.mark.django_db
class TestScopedUserAdmin:
    """A tenant-scoped user is confined to their own tenant."""

    def test_sees_only_own_rows(self, enforce_rls, client, scoped_admin_a, sample_orders):
        client.force_login(scoped_admin_a)
        body = client.get("/admin/test_app/order/").content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" not in body

    def test_tenant_fk_hidden_on_add(self, enforce_rls, client, scoped_admin_a):
        client.force_login(scoped_admin_a)
        body = client.get("/admin/test_app/order/add/").content.decode()
        assert 'name="product"' in body
        assert 'name="tenant"' not in body

    def test_add_auto_assigns_tenant(self, enforce_rls, client, scoped_admin_a, tenant_a):
        client.force_login(scoped_admin_a)
        response = client.post(
            "/admin/test_app/order/add/",
            {"product": "Owned", "amount": "3.50", "_save": "Save"},
        )
        assert response.status_code == 302
        with admin_context():
            assert Order.objects.get(product="Owned").tenant_id == tenant_a.pk

    def test_cannot_open_other_tenant_object(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        b_order = sample_orders["b1"]
        response = client.get(f"/admin/test_app/order/{b_order.pk}/change/")
        # Invisible under tenant A -> admin redirects away instead of showing it.
        assert response.status_code == 302

    def test_can_open_own_object(self, enforce_rls, client, scoped_admin_a, sample_orders):
        client.force_login(scoped_admin_a)
        a_order = sample_orders["a1"]
        response = client.get(f"/admin/test_app/order/{a_order.pk}/change/")
        assert response.status_code == 200
        assert "Widget A1" in response.content.decode()

    def test_cannot_delete_other_tenant_object(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        response = client.get(f"/admin/test_app/order/{sample_orders['b1'].pk}/delete/")
        # Invisible under tenant A -> admin redirects (302) or 404s; never a 500.
        assert response.status_code in {302, 404}

    def test_can_view_own_history(self, enforce_rls, client, scoped_admin_a, sample_orders):
        client.force_login(scoped_admin_a)
        response = client.get(f"/admin/test_app/order/{sample_orders['a1'].pk}/history/")
        assert response.status_code == 200

    def test_no_switcher_rendered(self, enforce_rls, client, scoped_admin_a, sample_orders):
        client.force_login(scoped_admin_a)
        body = client.get("/admin/test_app/order/").content.decode()
        assert "By tenant" not in body


@pytest.mark.integration
@pytest.mark.django_db
class TestScopedUserWrites:
    """A scoped user's writes (POST) are confined to their own tenant.

    The changelist tests prove *reads* are scoped; these prove the headline
    guarantee for *writes* -- a scoped user cannot change, delete, or bulk-delete
    another tenant's row even when its primary key is known.
    """

    def test_can_post_change_to_own_object(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        a_order = sample_orders["a1"]
        response = client.post(
            f"/admin/test_app/order/{a_order.pk}/change/",
            {"product": "Updated A1", "amount": "10.00", "_save": "Save"},
        )
        assert response.status_code == 302
        with admin_context():
            assert Order.objects.get(pk=a_order.pk).product == "Updated A1"

    def test_cannot_post_change_to_other_tenant_object(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        b_order = sample_orders["b1"]
        response = client.post(
            f"/admin/test_app/order/{b_order.pk}/change/",
            {"product": "Hijacked", "amount": "1.00", "_save": "Save"},
        )
        assert response.status_code in {302, 404}  # invisible under tenant A
        with admin_context():
            assert Order.objects.get(pk=b_order.pk).product == "Gadget B1"  # unchanged

    def test_cannot_post_delete_other_tenant_object(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        b_order = sample_orders["b1"]
        response = client.post(f"/admin/test_app/order/{b_order.pk}/delete/", {"post": "yes"})
        assert response.status_code in {302, 404}
        with admin_context():
            assert Order.objects.filter(pk=b_order.pk).exists()  # survived

    def test_bulk_delete_action_cannot_reach_other_tenant(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        b_order = sample_orders["b1"]
        response = client.post(
            "/admin/test_app/order/",
            {
                "action": "delete_selected",
                "_selected_action": [str(b_order.pk)],
                "select_across": "0",
                "index": "0",
            },
        )
        # B's row is invisible to tenant A, so the action selects nothing.
        assert response.status_code in {200, 302}
        with admin_context():
            assert Order.objects.filter(pk=b_order.pk).exists()


@pytest.mark.integration
@pytest.mark.django_db
class TestFieldsetsRendering:
    """An admin that lists the tenant FK in explicit ``fieldsets`` still renders.

    ``OrderItemAdmin`` uses fieldsets naming the tenant FK. For a scoped user the
    FK is excluded from the form, so without ``get_fieldsets`` stripping it from
    the layout Django would raise ``KeyError`` (HTTP 500) at render time.
    """

    def test_scoped_user_add_form_renders_without_tenant(
        self, enforce_rls, client, scoped_admin_a
    ):
        client.force_login(scoped_admin_a)
        response = client.get("/admin/test_app/orderitem/add/")
        assert response.status_code == 200  # 500 (KeyError) before the fix
        assert 'name="tenant"' not in response.content.decode()

    def test_global_admin_add_form_shows_tenant(self, enforce_rls, client, superuser_admin):
        client.force_login(superuser_admin)
        response = client.get("/admin/test_app/orderitem/add/")
        assert response.status_code == 200
        assert 'name="tenant"' in response.content.decode()


@pytest.mark.integration
@pytest.mark.django_db
class TestFailClosed:
    """A non-admin user with no tenant is denied, not silently elevated."""

    def test_user_without_tenant_gets_403(self, enforce_rls, client, orphan_user):
        client.force_login(orphan_user)
        response = client.get("/admin/test_app/order/")
        assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.django_db
class TestAutocompleteScoping:
    """Related-field autocomplete is scoped via the per-request middleware context.

    Autocomplete is a site-level admin view (in Django >= 6.0 it is not even a
    ``ModelAdmin`` method), so RLSTenantModelAdmin cannot wrap it; the scoping
    comes from RLSTenantMiddleware establishing the request's RLS context, which
    ``RLSManager.get_queryset`` and the live policy then honour.
    """

    @override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS)
    def test_scoped_user_autocomplete_limited(
        self, enforce_rls, client, scoped_admin_a, sample_orders, tenant_a
    ):
        client.force_login(scoped_admin_a)
        response = client.get("/admin/autocomplete/", _AUTOCOMPLETE_ORDER)
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {str(sample_orders["a1"].pk), str(sample_orders["a2"].pk)}

    @override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS)
    def test_admin_autocomplete_sees_all(
        self, enforce_rls, client, superuser_admin, sample_orders
    ):
        client.force_login(superuser_admin)
        response = client.get("/admin/autocomplete/", _AUTOCOMPLETE_ORDER)
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {str(order.pk) for order in sample_orders.values()}


@pytest.mark.integration
@pytest.mark.django_db
class TestMiddlewareNesting:
    """The mixin's context nests cleanly over RLSTenantMiddleware."""

    @override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS)
    def test_scoped_user_still_isolated_with_middleware(
        self, enforce_rls, client, scoped_admin_a, sample_orders
    ):
        client.force_login(scoped_admin_a)
        body = client.get("/admin/test_app/order/").content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" not in body
