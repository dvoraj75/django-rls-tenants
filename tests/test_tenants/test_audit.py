"""Tests for django_rls_tenants.tenants.audit (#48) and the audit records of each source.

The DRF and Celery sources are covered in ``tests/test_contrib``.
"""

from __future__ import annotations

import logging
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.signals import request_finished
from django.db import connection
from django.test import AsyncClient, Client, override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.admin import RLSTenantModelAdmin
from django_rls_tenants.tenants.context import (
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
    with_rls_context,
)
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware, _mark_gucs_set
from django_rls_tenants.tenants.models import validate_cross_tenant_fks
from django_rls_tenants.tenants.state import (
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)
from tests.test_app.models import AdminUser, Assignment, Order, OrderItem, ProtectedUser

pytestmark = pytest.mark.django_db

_RLS = "django_rls_tenants.tenants.middleware.RLSTenantMiddleware"

_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    _RLS,
]


@pytest.fixture
def records(caplog):
    """Return a function listing the audit records logged so far."""
    caplog.set_level(logging.INFO, logger=audit.AUDIT_LOGGER_NAME)

    def audit_records():
        return [record for record in caplog.records if record.name == audit.AUDIT_LOGGER_NAME]

    return audit_records


def _events(records):
    """``(event, source, tenant_id, admin, user_id)`` of each record."""
    return [
        (r.rls_event, r.rls_source, r.rls_tenant_id, r.rls_admin, r.rls_user_id) for r in records
    ]


def _make_request(user=None):
    """A mock ``HttpRequest`` with an optional user."""
    request = MagicMock()
    if user is not None:
        request.user = user
    else:
        del request.user
    return request


def _web_user(username, *, tenant=None, rls_admin=False):
    """A real ``AUTH_USER_MODEL`` user, for requests through Django's test clients."""
    return AdminUser.objects.create_user(
        username=username,
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant,
        rls_admin=rls_admin,
    )


# ---------------------------------------------------------------------------
# emit()
# ---------------------------------------------------------------------------


class TestEmit:
    def test_fields_are_record_attributes(self, records):
        audit.emit(
            audit.CONTEXT_ENTERED, source="middleware", tenant_id=42, admin=False, user_id=7
        )

        (record,) = records()
        assert record.levelno == logging.INFO
        assert record.rls_event == "context.entered"
        assert record.rls_source == "middleware"
        assert record.rls_tenant_id == 42
        assert record.rls_admin is False
        assert record.rls_user_id == 7

    def test_message_lists_the_fields_that_are_set(self, records):
        audit.emit(audit.CONTEXT_ENTERED, source="context_manager", admin=True, using="default")

        (record,) = records()
        assert (
            record.getMessage()
            == "context.entered source=context_manager admin=True using=default"
        )
        assert record.rls_tenant_id is None
        assert record.rls_user_id is None
        assert record.rls_using == "default"

    def test_nothing_is_logged_below_the_logger_level(self, caplog):
        caplog.set_level(logging.WARNING, logger=audit.AUDIT_LOGGER_NAME)
        audit.emit(audit.CONTEXT_ENTERED, source="middleware", tenant_id=1)
        assert not [r for r in caplog.records if r.name == audit.AUDIT_LOGGER_NAME]

    def test_denied_is_a_warning(self, records):
        audit.denied("queryset", "strict_mode", model="test_app.Order")

        (record,) = records()
        assert record.levelno == logging.WARNING
        assert record.rls_event == "context.denied"
        assert record.rls_reason == "strict_mode"
        assert record.rls_model == "test_app.Order"

    def test_the_audit_logger_is_a_child_of_the_library_logger(self):
        assert audit.audit_logger.parent is logging.getLogger("django_rls_tenants")


# ---------------------------------------------------------------------------
# Context managers, attribution, decorator
# ---------------------------------------------------------------------------


class TestContextManagers:
    def test_tenant_context_enters_and_exits(self, records):
        with tenant_context(5):
            assert _events(records()) == [("context.entered", "context_manager", 5, False, None)]

        assert _events(records())[1:] == [("context.exited", "context_manager", 5, False, None)]
        assert [r.rls_using for r in records()] == ["default", "default"]

    def test_admin_context_enters_and_exits(self, records):
        with admin_context():
            pass

        assert _events(records()) == [
            ("context.entered", "context_manager", None, True, None),
            ("context.exited", "context_manager", None, True, None),
        ]

    def test_exit_is_logged_when_the_block_raises(self, records):
        with pytest.raises(RuntimeError), tenant_context(5):
            raise RuntimeError

        assert [r.rls_event for r in records()] == ["context.entered", "context.exited"]

    def test_nested_blocks_are_logged_in_order(self, records):
        with tenant_context(5), admin_context():
            pass

        assert [(r.rls_event, r.rls_admin) for r in records()] == [
            ("context.entered", False),
            ("context.entered", True),
            ("context.exited", True),
            ("context.exited", False),
        ]

    def test_tenant_id_none_is_denied(self, records):
        with pytest.raises(NoTenantContextError):
            tenant_context(None).__enter__()

        (record,) = records()
        assert record.levelno == logging.WARNING
        assert (record.rls_event, record.rls_source, record.rls_reason) == (
            "context.denied",
            "context_manager",
            "tenant_id_none",
        )

    def test_failed_entry_logs_no_exit(self, records):
        with (
            patch("django_rls_tenants.tenants.context.set_guc", side_effect=RuntimeError),
            pytest.raises(RuntimeError),
            tenant_context(5),
        ):
            pass  # pragma: no cover - never entered

        assert records() == []

    def test_async_tenant_context(self, records):
        async def run():
            async with async_tenant_context(5):
                pass

        async_to_sync(run)()

        assert _events(records()) == [
            ("context.entered", "context_manager", 5, False, None),
            ("context.exited", "context_manager", 5, False, None),
        ]

    def test_async_admin_context(self, records):
        async def run():
            async with async_admin_context():
                pass

        async_to_sync(run)()

        assert _events(records()) == [
            ("context.entered", "context_manager", None, True, None),
            ("context.exited", "context_manager", None, True, None),
        ]

    def test_async_tenant_id_none_is_denied(self, records):
        async def run():
            async with async_tenant_context(None):
                pass  # pragma: no cover - never entered

        with pytest.raises(NoTenantContextError):
            async_to_sync(run)()

        assert [(r.rls_event, r.rls_reason) for r in records()] == [
            ("context.denied", "tenant_id_none"),
        ]


class TestAttribution:
    def test_attributed_names_the_source_and_user(self, records):
        user = SimpleNamespace(pk=9)
        with audit.attributed(tenant_context(5), source="celery", user=user):
            pass

        assert _events(records()) == [
            ("context.entered", "celery", 5, False, 9),
            ("context.exited", "celery", 5, False, 9),
        ]

    def test_blocks_nested_in_an_attributed_block_are_the_codes_own(self, records):
        with audit.attributed(tenant_context(5), source="admin"), admin_context():
            pass

        assert [(r.rls_event, r.rls_source) for r in records()] == [
            ("context.entered", "admin"),
            ("context.entered", "context_manager"),
            ("context.exited", "context_manager"),
            ("context.exited", "admin"),
        ]

    def test_silenced_block_logs_nothing(self, records):
        with audit.silenced(admin_context()), tenant_context(5):
            pass

        assert [(r.rls_event, r.rls_tenant_id) for r in records()] == [
            ("context.entered", 5),
            ("context.exited", 5),
        ]

    def test_silenced_tenant_id_none_logs_nothing(self, records):
        with pytest.raises(NoTenantContextError), audit.silenced(tenant_context(None)):
            pass  # pragma: no cover - never entered

        assert records() == []

    def test_async_attributed(self, records):
        async def run():
            async with audit.async_attributed(async_admin_context(), source="decorator"):
                pass

        async_to_sync(run)()

        assert [(r.rls_event, r.rls_source) for r in records()] == [
            ("context.entered", "decorator"),
            ("context.exited", "decorator"),
        ]


@with_rls_context
def _decorated(as_user):
    return "ok"


@with_rls_context
async def _adecorated(as_user):
    return "ok"


class TestDecorator:
    def test_sync_call_is_attributed_to_the_user(self, records, tenant_a_user):
        assert _decorated(tenant_a_user) == "ok"

        tenant = tenant_a_user.rls_tenant_id
        assert _events(records()) == [
            ("context.entered", "decorator", tenant, False, tenant_a_user.pk),
            ("context.exited", "decorator", tenant, False, tenant_a_user.pk),
        ]

    def test_async_call_is_attributed_to_the_user(self, records, admin_user):
        assert async_to_sync(_adecorated)(admin_user) == "ok"

        assert _events(records()) == [
            ("context.entered", "decorator", None, True, admin_user.pk),
            ("context.exited", "decorator", None, True, admin_user.pk),
        ]

    def test_tenantless_user_is_denied(self, records):
        user = SimpleNamespace(pk=3, is_tenant_admin=False, rls_tenant_id=None)
        with pytest.raises(NoTenantContextError):
            _decorated(user)

        assert [(r.rls_event, r.rls_source, r.rls_reason, r.rls_user_id) for r in records()] == [
            ("context.denied", "decorator", "user_without_tenant", 3),
        ]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


_ENTERED = "django_rls_tenants.tenants.middleware._audit_request_entered"


def _assert_no_context():
    """Nothing of a request's context is left on this thread or its connection."""
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False
    assert _get_context_gucs() is None
    assert get_guc("rls.current_tenant") is None
    assert get_guc("rls.is_admin") is None


class TestMiddleware:
    def test_sync_request_enters_and_exits(self, records, tenant_a_user):
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        mw(_make_request(user=tenant_a_user))

        tenant = tenant_a_user.rls_tenant_id
        assert _events(records()) == [
            ("context.entered", "middleware", tenant, False, tenant_a_user.pk),
            ("context.exited", "middleware", tenant, False, tenant_a_user.pk),
        ]

    def test_admin_request(self, records, admin_user):
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        mw(_make_request(user=admin_user))

        assert _events(records()) == [
            ("context.entered", "middleware", None, True, admin_user.pk),
            ("context.exited", "middleware", None, True, admin_user.pk),
        ]

    def test_anonymous_request_logs_nothing(self, records):
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        mw(_make_request())
        assert records() == []

    def test_exit_is_logged_once_after_process_exception(self, records, tenant_a_user):
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        mw.process_exception(request, RuntimeError())
        mw.process_response(request, MagicMock())

        assert [r.rls_event for r in records()] == ["context.entered", "context.exited"]

    def test_tenantless_user_is_denied(self, records):
        user = SimpleNamespace(
            pk=4,
            is_authenticated=True,
            is_tenant_admin=False,
            rls_tenant_id=None,
        )
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        with pytest.raises(NoTenantContextError):
            mw.process_request(_make_request(user=user))

        assert [(r.rls_event, r.rls_source, r.rls_reason, r.rls_user_id) for r in records()] == [
            ("context.denied", "middleware", "user_without_tenant", 4),
        ]

    def test_native_async_request_enters_and_exits(self, records, tenant_a_user):
        async def aget_response(request):
            return MagicMock()

        mw = RLSTenantMiddleware(get_response=aget_response)
        async_to_sync(mw)(_make_request(user=tenant_a_user))

        tenant = tenant_a_user.rls_tenant_id
        assert _events(records()) == [
            ("context.entered", "middleware", tenant, False, tenant_a_user.pk),
            ("context.exited", "middleware", tenant, False, tenant_a_user.pk),
        ]

    def test_native_async_exit_is_logged_once_after_process_exception(
        self, records, tenant_a_user
    ):
        async def aget_response(request):
            # Django calls the hook in a hop of its own.
            await sync_to_async(mw.process_exception, thread_sensitive=True)(
                request,
                RuntimeError(),
            )
            return MagicMock()

        mw = RLSTenantMiddleware(get_response=aget_response)
        async_to_sync(mw)(_make_request(user=tenant_a_user))

        assert [r.rls_event for r in records()] == ["context.entered", "context.exited"]

    def test_native_async_tenantless_user_is_denied(self, records):
        async def aget_response(request):
            return MagicMock()  # pragma: no cover - never reached

        user = SimpleNamespace(
            pk=4, is_authenticated=True, is_tenant_admin=False, rls_tenant_id=None
        )
        mw = RLSTenantMiddleware(get_response=aget_response)
        with pytest.raises(NoTenantContextError):
            async_to_sync(mw)(_make_request(user=user))

        assert [(r.rls_event, r.rls_reason) for r in records()] == [
            ("context.denied", "user_without_tenant"),
        ]

    def test_failing_entry_record_leaves_no_context(self, tenant_a_user):
        """A logging filter that raises fails the request like a failed GUC write: fail-closed."""
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        with (
            patch(_ENTERED, side_effect=RuntimeError),
            pytest.raises(RuntimeError),
        ):
            mw.process_request(_make_request(user=tenant_a_user))

        _assert_no_context()

    def test_native_async_failing_entry_record_leaves_no_context(self, tenant_a_user):
        async def aget_response(request):
            return MagicMock()  # pragma: no cover - never reached

        mw = RLSTenantMiddleware(get_response=aget_response)
        with (
            patch(_ENTERED, side_effect=RuntimeError),
            pytest.raises(RuntimeError),
        ):
            async_to_sync(mw)(_make_request(user=tenant_a_user))

        _assert_no_context()

    def test_hook_path_enters_and_exits(self, records, tenant_a_user):
        class HookMiddleware(RLSTenantMiddleware):
            def process_request(self, request):
                return super().process_request(request)

        async def aget_response(request):
            return MagicMock()

        async_to_sync(HookMiddleware(get_response=aget_response))(
            _make_request(user=tenant_a_user),
        )

        assert [(r.rls_event, r.rls_source) for r in records()] == [
            ("context.entered", "middleware"),
            ("context.exited", "middleware"),
        ]

    @override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS)
    def test_wsgi_request_logs_no_warning(self, records, tenant_a):
        """A normal request: one entry, one exit, and the safety net stays silent."""
        user = _web_user("web_a", tenant=tenant_a)
        client = Client()
        client.force_login(user)

        response = client.get("/rls/orders/")

        assert response.status_code == 200
        assert _events(records()) == [
            ("context.entered", "middleware", tenant_a.pk, False, user.pk),
            ("context.exited", "middleware", tenant_a.pk, False, user.pk),
        ]
        assert all(r.levelno == logging.INFO for r in records())

    @override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS)
    def test_asgi_request_logs_no_warning(self, records, tenant_a):
        user = _web_user("web_a", tenant=tenant_a)
        client = AsyncClient()
        client.force_login(user)

        response = async_to_sync(client.get)("/rls/async-orders/")

        assert response.status_code == 200
        assert [(r.rls_event, r.rls_source) for r in records()] == [
            ("context.entered", "middleware"),
            ("context.exited", "middleware"),
        ]
        assert all(r.levelno == logging.INFO for r in records())


class TestSafetyNet:
    def test_clearing_a_request_context_is_a_warning(self, records, tenant_a_user):
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        mw.process_request(_make_request(user=tenant_a_user))  # process_response never runs

        request_finished.send(sender=self.__class__)

        cleared = [r for r in records() if r.rls_event == "gucs.cleared"]
        assert [(r.levelno, r.rls_source, r.rls_databases) for r in cleared] == [
            (logging.WARNING, "safety_net", ["default"]),
        ]

    def test_nothing_to_clear_logs_nothing(self, records):
        request_finished.send(sender=self.__class__)
        assert records() == []

    def test_failed_clear_logs_no_gucs_cleared(self, records):
        _mark_gucs_set()
        with patch(
            "django_rls_tenants.apps._clear_finished_request_gucs",
            side_effect=RuntimeError,
        ):
            request_finished.send(sender=self.__class__)

        assert records() == []


# ---------------------------------------------------------------------------
# Queryset: strict mode, for_user()
# ---------------------------------------------------------------------------


class TestQueryset:
    def test_strict_mode_refusal_is_denied(self, records):
        with (
            override_settings(
                RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "STRICT_MODE": True}
            ),
            pytest.raises(NoTenantContextError),
        ):
            list(Order.objects.all())

        assert [
            (r.levelno, r.rls_event, r.rls_source, r.rls_reason, r.rls_model) for r in records()
        ] == [(logging.WARNING, "context.denied", "queryset", "strict_mode", "test_app.Order")]

    def test_for_user_scopes_log_nothing(self, records, tenant_a_user, sample_orders):
        assert len(list(Order.objects.for_user(tenant_a_user))) == 2
        assert Order.objects.for_user(tenant_a_user).count() == 2
        assert records() == []

    def test_for_user_tenantless_user_is_denied(self, records):
        user = SimpleNamespace(pk=6, is_tenant_admin=True, rls_tenant_id=None)
        queryset = Order.objects.for_user(user)
        user.is_tenant_admin = False  # lost the admin flag after for_user()

        with pytest.raises(NoTenantContextError):
            list(queryset)

        assert [(r.rls_event, r.rls_source, r.rls_reason, r.rls_user_id) for r in records()] == [
            ("context.denied", "for_user", "user_without_tenant", 6),
        ]


# ---------------------------------------------------------------------------
# Cross-tenant foreign keys
# ---------------------------------------------------------------------------


class TestCrossTenantForeignKeys:
    def test_target_of_another_tenant_is_rejected(self, records, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order_id=sample_orders["b1"].pk, description="x")

        with pytest.raises(ValidationError):
            validate_cross_tenant_fks(item)

        (record,) = records()  # the lookup's own admin_context() is not logged
        assert record.levelno == logging.WARNING
        assert record.rls_event == "fk.cross_tenant_rejected"
        assert record.rls_source == "validation"
        assert record.rls_tenant_id == tenant_a.pk
        assert record.rls_model == "test_app.OrderItem"
        assert record.rls_fields == ["order"]

    def test_target_without_a_tenant_is_rejected(self, records, tenant_a):
        with admin_context():
            assignee = ProtectedUser.objects.create(email="nobody@example.com", tenant=None)
        assignment = Assignment(tenant=tenant_a, assignee=assignee, title="x")

        with pytest.raises(ValidationError):
            validate_cross_tenant_fks(assignment)

        cross = [r for r in records() if r.rls_event == "fk.cross_tenant_rejected"]
        assert [r.rls_fields for r in cross] == [["assignee"]]

    def test_missing_target_is_not_a_cross_tenant_reference(self, records, tenant_a):
        item = OrderItem(tenant=tenant_a, order_id=999_999, description="x")

        with pytest.raises(ValidationError):
            validate_cross_tenant_fks(item)

        assert records() == []

    def test_target_hidden_by_rls_is_logged(self, records, enforce_rls, tenant_a, sample_orders):
        """Django rejects a target RLS hides as not found first; the record is logged anyway."""
        item = OrderItem(tenant=tenant_a, order_id=sample_orders["b1"].pk, description="x")

        with audit.silenced(tenant_context(tenant_a.pk)), pytest.raises(ValidationError) as exc:
            item.full_clean()

        # Django's own error, unchanged.
        assert exc.value.message_dict == {
            "order": [f"order instance with id {sample_orders['b1'].pk} is not a valid choice."],
        }
        assert [(r.rls_event, r.rls_source, r.rls_fields) for r in records()] == [
            ("fk.cross_tenant_rejected", "validation", ["order"]),
        ]

    def test_missing_target_hidden_by_rls_logs_nothing(self, records, enforce_rls, tenant_a):
        item = OrderItem(tenant=tenant_a, order_id=999_999, description="x")

        with audit.silenced(tenant_context(tenant_a.pk)), pytest.raises(ValidationError):
            item.full_clean()

        assert records() == []

    def test_no_lookup_when_the_record_is_not_logged(
        self, caplog, enforce_rls, tenant_a, sample_orders
    ):
        item = OrderItem(tenant=tenant_a, order_id=sample_orders["b1"].pk, description="x")

        def queries_of_full_clean():
            with (
                audit.silenced(tenant_context(tenant_a.pk)),
                CaptureQueriesContext(connection) as queries,
                pytest.raises(ValidationError),
            ):
                item.full_clean()
            return len(queries)

        caplog.set_level(logging.ERROR, logger=audit.AUDIT_LOGGER_NAME)
        silent = queries_of_full_clean()
        caplog.set_level(logging.WARNING, logger=audit.AUDIT_LOGGER_NAME)
        logged = queries_of_full_clean()

        assert logged > silent

    def test_target_of_the_same_tenant_logs_nothing(self, records, tenant_a, sample_orders):
        validate_cross_tenant_fks(
            OrderItem(tenant=tenant_a, order_id=sample_orders["a1"].pk, description="x"),
        )
        assert records() == []


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAdmin:
    def test_changelist_is_attributed_to_the_admin(self, records, enforce_rls, client, tenant_a):
        user = AdminUser.objects.create_superuser(
            username="staff_a",
            password="pw",  # noqa: S106  -- test-only credential
            tenant=tenant_a,
            rls_admin=False,
        )
        client.force_login(user)

        assert client.get("/admin/test_app/order/").status_code == 200

        admin_records = [r for r in records() if r.rls_source == "admin"]
        assert _events(admin_records) == [
            ("context.entered", "admin", tenant_a.pk, False, user.pk),
            ("context.exited", "admin", tenant_a.pk, False, user.pk),
        ]

    def test_tenantless_user_is_denied(self, records):
        user = SimpleNamespace(pk=8, is_tenant_admin=False, rls_tenant_id=None)
        model_admin = RLSTenantModelAdmin(Order, MagicMock())

        with pytest.raises(PermissionDenied):
            model_admin._rls_context(SimpleNamespace(user=user))

        assert [(r.rls_event, r.rls_source, r.rls_reason, r.rls_user_id) for r in records()] == [
            ("context.denied", "admin", "user_without_tenant", 8),
        ]


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCommands:
    def test_run_as_tenant(self, records, tenant_a):
        call_command("run_as_tenant", "--tenant", str(tenant_a.pk), "rls_probe", stdout=StringIO())

        # Resolving the tenant in admin_context() is the command's own lookup.
        assert [(r.rls_event, r.rls_source, r.rls_tenant_id) for r in records()] == [
            ("context.entered", "command", tenant_a.pk),
            ("context.exited", "command", tenant_a.pk),
        ]

    def test_run_as_tenant_admin(self, records):
        call_command("run_as_tenant", "--admin", "rls_probe", stdout=StringIO())

        assert [(r.rls_event, r.rls_source, r.rls_admin) for r in records()] == [
            ("context.entered", "command", True),
            ("context.exited", "command", True),
        ]

    def test_run_for_all_tenants(self, records, tenant_a, tenant_b):
        call_command("run_for_all_tenants", "rls_probe", stdout=StringIO(), stderr=StringIO())

        # Listing the tenants in admin_context() is the command's own lookup.
        assert [(r.rls_event, r.rls_source, r.rls_tenant_id) for r in records()] == [
            ("context.entered", "command", tenant_a.pk),
            ("context.exited", "command", tenant_a.pk),
            ("context.entered", "command", tenant_b.pk),
            ("context.exited", "command", tenant_b.pk),
        ]
