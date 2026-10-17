"""Tests for django_rls_tenants.tenants.selection (#47).

Unit tests of the tenant selection of a ``MultiTenantUser``: which users select,
how a request's tenant is resolved (header, then session, then ``rls_tenant_id``),
the refusals and their audit records, ``switch_tenant()`` /
``clear_tenant_selection()``, and the ``TENANT_HEADER`` / ``TENANT_SESSION_KEY``
settings. The middleware, DRF and admin request cycles are covered in
``tests/test_integration/test_multi_tenant_users.py``, ``tests/test_contrib/test_drf.py``
and ``tests/test_tenants/test_admin.py``.
"""

from __future__ import annotations

import logging
import uuid
import warnings
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import models
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.utils.functional import SimpleLazyObject

import django_rls_tenants
from django_rls_tenants import tenants
from django_rls_tenants.exceptions import RLSConfigurationError
from django_rls_tenants.tenants import audit, selection
from django_rls_tenants.tenants.conf import RLSTenantsConfig
from django_rls_tenants.tenants.context import _resolve_user_guc_vars
from django_rls_tenants.tenants.selection import (
    NOT_SELECTABLE_MESSAGE,
    _may_select,
    _other_tenants,
    _request_tenant_id,
    _selectable,
    _selects_tenants,
    _vary_on_tenant_header,
    clear_tenant_selection,
    switch_tenant,
)
from django_rls_tenants.tenants.types import MultiTenantUser, TenantUser

pytestmark = pytest.mark.django_db

_SETTINGS = {"TENANT_MODEL": "test_app.Tenant"}

# A session logged in as the ``_member()`` stand-in (Django's ``login()`` stores its pk).
_AUTH = {"_auth_user_id": "7"}


def _member(default: int | None = 1, tenants: Any = (2, 3)) -> SimpleNamespace:
    """A ``MultiTenantUser`` stand-in: default tenant ``default``, members of ``tenants``."""
    return SimpleNamespace(
        pk=7,
        is_authenticated=True,
        is_tenant_admin=False,
        rls_tenant_id=default,
        rls_tenant_ids=tenants,
    )


def _request(
    user: Any = None,
    *,
    headers: dict[str, str] | None = None,
    session: dict[str, Any] | None = None,
):
    """A ``RequestFactory`` request with ``user`` and a plain-dict session (``None``: none)."""
    request = RequestFactory().get("/", headers=headers or {})
    request.user = user
    if session is not None:
        request.session = session  # type: ignore[attr-defined]
    return request


@pytest.fixture
def records(caplog):
    """Return a function listing the audit records logged so far."""
    caplog.set_level(logging.INFO, logger=audit.AUDIT_LOGGER_NAME)

    def audit_records():
        return [record for record in caplog.records if record.name == audit.AUDIT_LOGGER_NAME]

    return audit_records


class _CountingMember:
    """A ``MultiTenantUser`` that counts the reads of ``rls_tenant_ids``."""

    is_tenant_admin = False
    rls_tenant_id = 1

    def __init__(self) -> None:
        self.reads = 0

    @property
    def rls_tenant_ids(self) -> frozenset[int]:
        self.reads += 1
        return frozenset({2})


# ---------------------------------------------------------------------------
# Which users select
# ---------------------------------------------------------------------------


class TestSelectsTenants:
    def test_member_selects(self):
        assert _selects_tenants(_member()) is True

    def test_tenant_admin_does_not(self):
        admin = SimpleNamespace(is_tenant_admin=True, rls_tenant_id=None, rls_tenant_ids={1})
        assert _selects_tenants(admin) is False

    def test_single_tenant_user_does_not(self):
        user = SimpleNamespace(is_tenant_admin=False, rls_tenant_id=1)
        assert isinstance(user, TenantUser)
        assert _selects_tenants(user) is False

    def test_protocol_check_reads_no_membership(self):
        user = _CountingMember()
        assert isinstance(user, MultiTenantUser)
        assert _selects_tenants(user) is True
        assert user.reads == 0

    def test_lazy_user_is_recognised_without_reading_memberships(self):
        """``request.user`` is a ``SimpleLazyObject``."""
        user = _CountingMember()
        lazy = SimpleLazyObject(lambda: user)
        assert _selects_tenants(lazy) is True
        assert user.reads == 0


class TestSelectable:
    def test_memberships_and_default(self):
        assert _selectable(_member(1, [2, "3"])) == {1, 2, 3}

    def test_without_a_default(self):
        assert _selectable(_member(None, [2])) == {2}

    def test_other_tenants_leave_out_the_default(self):
        assert _other_tenants(_member(1, [1, 2])) == {2}
        assert _other_tenants(_member(1, [1])) == set()
        assert _other_tenants(_member(None, [2])) == {2}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(2, 2), ("2", 2), (" 3", 3), (1, 1), ("9", None), ("abc", None), ("", None)],
    )
    def test_may_select(self, value, expected):
        assert _may_select(_member(1, [2, 3]), value) == expected

    def test_none_is_never_selectable(self):
        assert _may_select(_member(None, [None, 2]), None) is None


# ---------------------------------------------------------------------------
# The request's tenant
# ---------------------------------------------------------------------------


class TestRequestTenantId:
    def test_nothing_selected_is_the_default(self):
        request = _request(headers={}, session={**_AUTH})
        assert _request_tenant_id(request, _member(1), source="middleware") == 1

    def test_nothing_selected_and_no_default_is_none(self):
        request = _request(session={**_AUTH})
        assert _request_tenant_id(request, _member(None), source="middleware") is None

    def test_header_selects(self):
        request = _request(headers={"X-Tenant": "2"}, session={**_AUTH})
        assert _request_tenant_id(request, _member(), source="middleware") == 2

    def test_header_may_select_the_default(self):
        request = _request(headers={"X-Tenant": "1"})
        assert _request_tenant_id(request, _member(1, []), source="middleware") == 1

    def test_session_selects(self):
        request = _request(session={**_AUTH, "rls_tenant": "3"})
        assert _request_tenant_id(request, _member(), source="middleware") == 3

    def test_header_wins_over_session(self):
        request = _request(headers={"X-Tenant": "2"}, session={**_AUTH, "rls_tenant": "3"})
        assert _request_tenant_id(request, _member(), source="middleware") == 2
        assert request.session == {**_AUTH, "rls_tenant": "3"}

    @pytest.mark.parametrize("value", ["9", "abc", "", " ", "2.0", "-1"])
    def test_header_not_of_the_user_is_refused(self, value, records):
        request = _request(headers={"X-Tenant": value}, session={**_AUTH, "rls_tenant": "3"})

        with pytest.raises(PermissionDenied) as excinfo:
            _request_tenant_id(request, _member(), source="drf")

        assert str(excinfo.value) == NOT_SELECTABLE_MESSAGE
        # Neither the session's selection nor the default replaces it.
        assert request.session == {**_AUTH, "rls_tenant": "3"}
        (record,) = records()
        assert record.levelno == logging.WARNING
        assert record.rls_event == audit.CONTEXT_DENIED
        assert record.rls_reason == "tenant_not_selectable"
        assert record.rls_source == "drf"
        assert record.rls_user_id == 7
        assert record.rls_selected == value
        assert record.rls_via == "header"

    def test_stale_session_selection_is_refused_and_removed(self, records):
        request = _request(session={**_AUTH, "rls_tenant": "9", "other": 1})

        with pytest.raises(PermissionDenied, match=NOT_SELECTABLE_MESSAGE):
            _request_tenant_id(request, _member(), source="middleware")

        assert request.session == {**_AUTH, "other": 1}
        # The next request runs in the default tenant.
        assert _request_tenant_id(request, _member(), source="middleware") == 1
        (record,) = records()
        assert record.rls_reason == "tenant_not_selectable"
        assert record.rls_source == "middleware"
        assert record.rls_selected == "9"
        assert record.rls_via == "session"

    def test_without_a_session_the_default_applies(self):
        request = _request()  # no SessionMiddleware
        assert not hasattr(request, "session")
        assert _request_tenant_id(request, _member(), source="middleware") == 1

    def test_header_turned_off(self):
        request = _request(headers={"X-Tenant": "9"}, session={**_AUTH, "rls_tenant": "2"})
        with override_settings(RLS_TENANTS={**_SETTINGS, "TENANT_HEADER": None}):
            assert _request_tenant_id(request, _member(), source="middleware") == 2

    def test_session_turned_off(self):
        request = _request(session={**_AUTH, "rls_tenant": "9"})
        with override_settings(RLS_TENANTS={**_SETTINGS, "TENANT_SESSION_KEY": None}):
            assert _request_tenant_id(request, _member(), source="middleware") == 1
        assert request.session == {**_AUTH, "rls_tenant": "9"}

    def test_custom_names(self):
        settings = {**_SETTINGS, "TENANT_HEADER": "X-Org", "TENANT_SESSION_KEY": "org"}
        with override_settings(RLS_TENANTS=settings):
            by_header = _request(headers={"X-Org": "2", "X-Tenant": "9"})
            by_session = _request(session={**_AUTH, "org": "3", "rls_tenant": "9"})
            assert _request_tenant_id(by_header, _member(), source="middleware") == 2
            assert _request_tenant_id(by_session, _member(), source="middleware") == 3

    def test_memberships_read_only_with_a_selection(self):
        user = _CountingMember()
        assert _request_tenant_id(_request(session={**_AUTH}), user, source="middleware") == 1
        assert user.reads == 0


# ---------------------------------------------------------------------------
# switch_tenant() / clear_tenant_selection()
# ---------------------------------------------------------------------------


class TestSwitchTenant:
    def test_stores_the_selection(self, records):
        request = _request(_member(), session={**_AUTH, "rls_tenant": "3"})

        assert switch_tenant(request, "2") == 2

        assert request.session == {**_AUTH, "rls_tenant": "2"}
        (record,) = records()
        assert record.levelno == logging.INFO
        assert record.rls_event == audit.TENANT_SWITCHED
        assert record.rls_source == "selection"
        assert record.rls_tenant_id == 2
        assert record.rls_admin is False
        assert record.rls_user_id == 7
        assert record.rls_previous == "3"
        assert record.getMessage() == (
            "tenant.switched source=selection tenant_id=2 admin=False user_id=7 previous=3"
        )

    def test_accepts_an_int_and_the_default(self):
        request = _request(_member(1, [2]), session={**_AUTH})
        assert switch_tenant(request, 1) == 1
        assert request.session == {**_AUTH, "rls_tenant": "1"}

    @pytest.mark.parametrize("value", [9, "9", "abc", ""])
    def test_refuses_a_tenant_of_another(self, value, records):
        request = _request(_member(), session={**_AUTH, "rls_tenant": "3"})

        with pytest.raises(PermissionDenied, match=NOT_SELECTABLE_MESSAGE):
            switch_tenant(request, value)

        assert request.session == {**_AUTH, "rls_tenant": "3"}
        (record,) = records()
        assert record.rls_event == audit.CONTEXT_DENIED
        assert record.rls_reason == "tenant_not_selectable"
        assert record.rls_source == "selection"
        assert record.rls_via == "switch"

    @pytest.mark.parametrize(
        "user",
        [
            AnonymousUser(),
            None,
            SimpleNamespace(pk=1, is_authenticated=True, is_tenant_admin=False, rls_tenant_id=2),
            SimpleNamespace(
                pk=1,
                is_authenticated=True,
                is_tenant_admin=True,
                rls_tenant_id=None,
                rls_tenant_ids={2},
            ),
        ],
        ids=["anonymous", "no-user", "single-tenant", "tenant-admin"],
    )
    def test_refuses_a_user_who_does_not_select(self, user):
        request = _request(user, session={**_AUTH})
        with pytest.raises(PermissionDenied):
            switch_tenant(request, 2)
        assert request.session == _AUTH

    def test_session_turned_off(self):
        request = _request(_member(), session={**_AUTH})
        with (
            override_settings(RLS_TENANTS={**_SETTINGS, "TENANT_SESSION_KEY": None}),
            pytest.raises(RLSConfigurationError, match="TENANT_SESSION_KEY"),
        ):
            switch_tenant(request, 2)

    def test_without_a_session(self):
        with pytest.raises(RLSConfigurationError, match="SessionMiddleware"):
            switch_tenant(_request(_member()), 2)


class TestClearTenantSelection:
    def test_removes_the_selection(self, records):
        request = _request(_member(), session={**_AUTH, "rls_tenant": "2", "other": 1})

        clear_tenant_selection(request)

        assert request.session == {**_AUTH, "other": 1}
        (record,) = records()
        assert record.rls_event == audit.TENANT_SWITCHED
        assert record.rls_tenant_id is None
        assert record.rls_previous == "2"
        assert record.rls_user_id == 7

    def test_without_a_selection_logs_nothing(self, records):
        request = _request(_member(), session={**_AUTH})
        clear_tenant_selection(request)
        assert request.session == _AUTH
        assert records() == []

    def test_session_turned_off(self):
        with (
            override_settings(RLS_TENANTS={**_SETTINGS, "TENANT_SESSION_KEY": None}),
            pytest.raises(RLSConfigurationError),
        ):
            clear_tenant_selection(_request(_member(), session={**_AUTH}))


# ---------------------------------------------------------------------------
# The resolved tenant in the GUC mapping, settings, exports
# ---------------------------------------------------------------------------


class TestResolveUserGucVars:
    def test_selected_tenant_replaces_the_default(self):
        assert _resolve_user_guc_vars(_member(1), tenant_id=2) == {
            "rls.is_admin": "false",
            "rls.current_tenant": "2",
        }

    def test_admin_ignores_it(self):
        admin = SimpleNamespace(is_tenant_admin=True, rls_tenant_id=None)
        assert _resolve_user_guc_vars(admin, tenant_id=2)["rls.is_admin"] == "true"


class TestSettings:
    def test_defaults(self):
        conf = RLSTenantsConfig()
        with override_settings(RLS_TENANTS=_SETTINGS):
            assert conf.TENANT_HEADER == "X-Tenant"
            assert conf.TENANT_SESSION_KEY == "rls_tenant"

    def test_none_turns_them_off(self):
        conf = RLSTenantsConfig()
        settings = {**_SETTINGS, "TENANT_HEADER": None, "TENANT_SESSION_KEY": None}
        with override_settings(RLS_TENANTS=settings), warnings.catch_warnings():
            warnings.simplefilter("error")  # known keys: no unknown-key warning
            assert conf.TENANT_HEADER is None
            assert conf.TENANT_SESSION_KEY is None

    @pytest.mark.parametrize("key", ["TENANT_HEADER", "TENANT_SESSION_KEY"])
    @pytest.mark.parametrize("value", ["", "  ", 1, ["X-Tenant"]])
    def test_invalid_values(self, key, value):
        conf = RLSTenantsConfig()
        with (
            override_settings(RLS_TENANTS={**_SETTINGS, key: value}),
            pytest.raises(RLSConfigurationError, match=key),
        ):
            getattr(conf, key)


def test_public_api():
    assert django_rls_tenants.MultiTenantUser is MultiTenantUser
    assert django_rls_tenants.switch_tenant is switch_tenant
    assert django_rls_tenants.clear_tenant_selection is clear_tenant_selection
    assert tenants.MultiTenantUser is MultiTenantUser
    assert tenants.switch_tenant is switch_tenant
    assert tenants.clear_tenant_selection is clear_tenant_selection


# ---------------------------------------------------------------------------
# Review fixes: the session's own user, normalisation, the audit value, the header name
# ---------------------------------------------------------------------------


class TestSessionOfAnotherUser:
    """A selection in the session belongs to the user the session is logged in as."""

    @pytest.mark.parametrize("login", [{"_auth_user_id": "8"}, {}], ids=["other-user", "none"])
    def test_is_neither_used_nor_dropped(self, login):
        request = _request(session={**login, "rls_tenant": "9"})

        assert _request_tenant_id(request, _member(), source="drf") == 1
        assert request.session == {**login, "rls_tenant": "9"}

    def test_switch_is_refused(self, records):
        request = _request(_member(), session={"_auth_user_id": "8", "rls_tenant": "3"})

        with pytest.raises(PermissionDenied, match="TENANT_HEADER"):
            switch_tenant(request, 2)

        assert request.session == {"_auth_user_id": "8", "rls_tenant": "3"}
        (record,) = records()
        assert (record.rls_reason, record.rls_via) == ("tenant_not_selectable", "switch")

    def test_clear_does_nothing(self, records):
        request = _request(_member(), session={"_auth_user_id": "8", "rls_tenant": "3"})

        clear_tenant_selection(request)

        assert request.session == {"_auth_user_id": "8", "rls_tenant": "3"}
        assert records() == []

    def test_model_user_is_compared_by_its_pk_string(self, db):
        from tests.test_app.models import AdminUser  # noqa: PLC0415

        user = AdminUser.objects.create_user(username="m", password="pw")  # noqa: S106
        own = _request(session={"_auth_user_id": str(user.pk), "rls_tenant": "x"})
        other = _request(session={"_auth_user_id": str(user.pk + 1), "rls_tenant": "x"})
        with pytest.raises(PermissionDenied):
            _request_tenant_id(own, user, source="middleware")  # read, and refused
        assert _request_tenant_id(other, user, source="middleware") is None  # not read


class TestNormalisation:
    # int() reads "2_0" as 20: lenient, but the membership check and the GUC both use
    # the converted value, so it selects tenant 20 only for a member of tenant 20.
    @pytest.mark.parametrize(("value", "expected"), [("+2", 2), ("02", 2), ("2_0", None)])
    def test_integer_forms(self, value, expected):
        assert _may_select(_member(1, [2]), value) == expected

    def test_uuid_keys(self, monkeypatch):
        tenant = uuid.uuid4()
        monkeypatch.setattr(selection, "_tenant_pk", models.UUIDField().to_python)
        user = _member(None, [str(tenant)])

        assert _may_select(user, tenant.hex) == tenant
        assert _may_select(user, str(tenant).upper()) == tenant
        assert _may_select(user, str(uuid.uuid4())) is None
        request = _request(user, session={**_AUTH})
        assert switch_tenant(request, tenant.hex) == tenant
        assert request.session["rls_tenant"] == str(tenant)  # JSON-serialisable
        assert _request_tenant_id(request, user, source="middleware") == tenant


class TestAuditedValue:
    def test_a_long_value_is_cut(self, records):
        request = _request(headers={"X-Tenant": "9" * 5000})

        with pytest.raises(PermissionDenied):
            _request_tenant_id(request, _member(), source="middleware")

        (record,) = records()
        assert record.rls_selected == "9" * 64 + "..."


class TestVaryMarker:
    def test_header_dependency_is_recorded_for_the_response(self):
        request = _request(session={**_AUTH})
        _request_tenant_id(request, _member(), source="middleware")
        response = HttpResponse()

        _vary_on_tenant_header(request, response)

        assert response["Vary"] == "X-Tenant"

    def test_nothing_without_header_selection(self):
        request = _request(session={**_AUTH})
        response = HttpResponse()
        with override_settings(RLS_TENANTS={**_SETTINGS, "TENANT_HEADER": None}):
            _request_tenant_id(request, _member(), source="middleware")

        _vary_on_tenant_header(request, response)

        assert not response.has_header("Vary")


@pytest.mark.parametrize("value", ["X_Tenant", "X Tenant", "X-Tenant:", "Tenant\n"])
def test_header_name_must_be_one_clients_send(value):
    with (
        override_settings(RLS_TENANTS={**_SETTINGS, "TENANT_HEADER": value}),
        pytest.raises(RLSConfigurationError, match="letters, digits and hyphens"),
    ):
        _ = RLSTenantsConfig().TENANT_HEADER
