"""Tests for django_rls_tenants.tenants.audit (#48) and the audit records of each source.

The DRF and Celery sources are covered in ``tests/test_contrib``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.signals import request_finished
from django.db import DatabaseError, connection, transaction
from django.test import AsyncClient, Client, override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants import models as tenant_models
from django_rls_tenants.tenants.admin import RLSTenantModelAdmin
from django_rls_tenants.tenants.context import (
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
    with_rls_context,
)
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware, _mark_gucs_set
from django_rls_tenants.tenants.models import (
    _audit_rejected_values,
    _cross_tenant_errors,
    validate_cross_tenant_fks,
)
from django_rls_tenants.tenants.state import (
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)
from django_rls_tenants.tenants.testing import (
    aassert_rls_blocks_without_context,
    assert_rls_blocks_without_context,
)
from tests.test_app.models import (
    AdminUser,
    Assignment,
    NoteRevision,
    Order,
    OrderItem,
    ProtectedUser,
    Tag,
    Tenant,
)

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


class _RaisingFilter(logging.Filter):
    """A filter on the audit logger that fails, like a broken audit sink."""

    def filter(self, record):
        msg = "the audit sink is down"
        raise RuntimeError(msg)


@pytest.fixture
def failing_sink(caplog):
    """Make every audit record fail to be logged (a logging filter that raises)."""
    caplog.set_level(logging.INFO, logger=audit.AUDIT_LOGGER_NAME)
    sink = _RaisingFilter()
    audit.audit_logger.addFilter(sink)
    try:
        yield sink
    finally:
        audit.audit_logger.removeFilter(sink)


def _library_errors(caplog):
    """The ``ERROR`` records on the ``django_rls_tenants`` logger."""
    return [
        r for r in caplog.records if r.name == "django_rls_tenants" and r.levelno == logging.ERROR
    ]


def _staff(username, *, tenant=None, rls_admin=False):
    """A Django superuser (so it clears the admin's own permissions) with an RLS role."""
    return AdminUser.objects.create_superuser(
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

    def test_nothing_is_logged_and_log_is_not_called_at_error_level(self, caplog):
        caplog.set_level(logging.ERROR, logger=audit.AUDIT_LOGGER_NAME)

        with (
            patch.object(audit.audit_logger, "log") as log,
            tenant_context(5),
            admin_context(),
        ):
            audit.denied("queryset", "strict_mode")
            audit.emit(audit.GUCS_CLEARED, source="safety_net", level=logging.WARNING)

        log.assert_not_called()
        assert not [r for r in caplog.records if r.name == audit.AUDIT_LOGGER_NAME]

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ("middleware", "middleware"),
            (42, "42"),
            (True, "True"),
            ("Zoë", "Zoë"),
            ("a b admin=True", "'a b admin=True'"),
            ("a=b", "'a=b'"),
            ("tab\there", "'tab\\there'"),
            ("x\nforged", "'x\\nforged'"),
            ("nul\x00", "'nul\\x00'"),
        ],
    )
    def test_message_quotes_a_string_that_could_forge_a_field(self, records, value, shown):
        audit.emit(audit.CONTEXT_ENTERED, source="celery", tenant_id=value)

        (record,) = records()
        assert record.getMessage() == f"context.entered source=celery tenant_id={shown}"
        assert record.rls_tenant_id == value  # the extra keeps the raw value

    def test_a_forged_tenant_id_cannot_add_a_field_or_a_line(self, records):
        with tenant_context("a b admin=True"):
            pass
        with tenant_context("x\nforged"):
            pass

        entered = [r for r in records() if r.rls_event == "context.entered"]
        first = "context.entered source=context_manager tenant_id='a b admin=True'"
        second = "context.entered source=context_manager tenant_id='x\\nforged'"
        assert [r.getMessage() for r in entered] == [
            f"{first} admin=False using=default",
            f"{second} admin=False using=default",
        ]
        assert [r.rls_tenant_id for r in entered] == ["a b admin=True", "x\nforged"]
        assert all("\n" not in r.getMessage() for r in records())
        assert all(r.rls_admin is False for r in entered)

    def test_a_record_that_cannot_be_logged_is_dropped(self, caplog, failing_sink):
        audit.emit(audit.CONTEXT_ENTERED, source="middleware", tenant_id=1)
        audit.denied("queryset", "strict_mode")

        assert not [r for r in caplog.records if r.name == audit.AUDIT_LOGGER_NAME]
        failures = _library_errors(caplog)
        assert [r.getMessage() for r in failures] == [
            "Audit record context.entered could not be logged.",
            "Audit record context.denied could not be logged.",
        ]
        assert failures[0].exc_info[0] is RuntimeError

    def test_a_value_that_cannot_be_rendered_is_dropped(self, caplog, records):
        class Unprintable:
            def __str__(self):
                msg = "no text"
                raise TypeError(msg)

        audit.emit(audit.CONTEXT_ENTERED, source="middleware", tenant_id=Unprintable())

        assert records() == []
        assert len(_library_errors(caplog)) == 1

    def test_a_failure_to_report_the_failure_is_swallowed(self, failing_sink):
        with patch.object(audit, "_logger") as library_logger:
            library_logger.exception.side_effect = RuntimeError("the library logger is down")
            audit.emit(audit.CONTEXT_ENTERED, source="middleware")

        library_logger.exception.assert_called_once()

    def test_tenant_context_works_with_a_failing_sink(self, failing_sink):
        with tenant_context(5):
            assert get_current_tenant_id() == 5
            assert get_guc("rls.current_tenant") == "5"

        _assert_no_context()

    def test_admin_context_works_with_a_failing_sink(self, failing_sink):
        with admin_context():
            assert get_rls_context_active() is True

        _assert_no_context()

    def test_async_context_works_with_a_failing_sink(self, failing_sink):
        async def run():
            async with async_tenant_context(5):
                assert get_current_tenant_id() == 5

        async_to_sync(run)()

        _assert_no_context()


# ---------------------------------------------------------------------------
# Context managers, attribution, decorator
# ---------------------------------------------------------------------------


def _use_tenant_context():
    with tenant_context(5):
        pass


def _use_admin_context():
    with admin_context():
        pass


def _use_async_tenant_context():
    async def run():
        async with async_tenant_context(5):
            pass

    async_to_sync(run)()


def _use_async_admin_context():
    async def run():
        async with async_admin_context():
            pass

    async_to_sync(run)()


_CONTEXT = "django_rls_tenants.tenants.context"

# (how the manager is used, what fails its entry)
_MANAGERS = [
    pytest.param(_use_tenant_context, f"{_CONTEXT}.set_guc", id="tenant_context"),
    pytest.param(_use_admin_context, f"{_CONTEXT}.set_guc", id="admin_context"),
    pytest.param(
        _use_async_tenant_context, f"{_CONTEXT}._enter_tenant_gucs", id="async_tenant_context"
    ),
    pytest.param(
        _use_async_admin_context, f"{_CONTEXT}._enter_tenant_gucs", id="async_admin_context"
    ),
]


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

    @pytest.mark.parametrize(("use", "entry"), _MANAGERS)
    def test_failed_entry_logs_no_exit_of_any_manager(self, records, use, entry):
        with patch(entry, side_effect=RuntimeError), pytest.raises(RuntimeError):
            use()

        assert records() == []

    @pytest.mark.parametrize(("use", "entry"), _MANAGERS)
    def test_exit_is_logged_when_the_restore_raises(self, records, use, entry):
        with (
            patch(f"{_CONTEXT}._restore_tenant_gucs", side_effect=RuntimeError),
            pytest.raises(RuntimeError),
        ):
            use()

        assert [r.rls_event for r in records()] == ["context.entered", "context.exited"]

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

    def test_base_exception_skipping_process_response_logs_one_exit(self, records, tenant_a_user):
        """Not an ``Exception``: it skips ``process_response`` (WSGI); one exit is logged."""

        def interrupted(request):
            raise KeyboardInterrupt

        mw = RLSTenantMiddleware(get_response=interrupted)
        with pytest.raises(KeyboardInterrupt):
            mw(_make_request(user=tenant_a_user))

        tenant = tenant_a_user.rls_tenant_id
        assert _events(records()) == [
            ("context.entered", "middleware", tenant, False, tenant_a_user.pk),
            ("context.exited", "middleware", tenant, False, tenant_a_user.pk),
        ]
        _assert_no_context()

    def test_request_with_a_failing_sink_leaves_no_context(self, failing_sink, tenant_a_user):
        seen = []

        def view(request):
            seen.append(get_current_tenant_id())
            return MagicMock()

        RLSTenantMiddleware(get_response=view)(_make_request(user=tenant_a_user))

        assert seen == [tenant_a_user.rls_tenant_id]
        _assert_no_context()

    def test_native_async_request_with_a_failing_sink_leaves_no_context(
        self, failing_sink, tenant_a_user
    ):
        async def aget_response(request):
            return MagicMock()

        async_to_sync(RLSTenantMiddleware(get_response=aget_response))(
            _make_request(user=tenant_a_user),
        )

        _assert_no_context()

    @pytest.mark.parametrize("use_local_set", [False, True], ids=["session", "local"])
    def test_wsgi_view_exception_logs_one_exit(self, records, tenant_a, use_local_set):
        """A view that raises: Django answers 500, and the request's exit is logged once."""
        user = _web_user("web_a", tenant=tenant_a)
        client = Client(raise_request_exception=False)
        client.force_login(user)

        with override_settings(
            MIDDLEWARE=_MIDDLEWARE_WITH_RLS,
            RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": use_local_set},
        ):
            response = client.get("/rls/boom/")

        assert response.status_code == 500
        assert _events(records()) == [
            ("context.entered", "middleware", tenant_a.pk, False, user.pk),
            ("context.exited", "middleware", tenant_a.pk, False, user.pk),
        ]
        _assert_no_context()

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
        assert [
            (r.levelno, r.rls_source, r.rls_databases, r.rls_tenant_id, r.rls_admin)
            for r in cleared
        ] == [
            (logging.WARNING, "safety_net", ["default"], tenant_a_user.rls_tenant_id, False),
        ]

    def test_clearing_an_admin_request_context_names_the_admin_context(self, records, admin_user):
        mw = RLSTenantMiddleware(get_response=lambda request: MagicMock())
        mw.process_request(_make_request(user=admin_user))

        request_finished.send(sender=self.__class__)

        [cleared] = [r for r in records() if r.rls_event == "gucs.cleared"]
        assert (cleared.rls_tenant_id, cleared.rls_admin) == (None, True)

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

    def test_failed_audit_lookup_loses_the_record_never_the_error(
        self, records, enforce_rls, tenant_a, sample_orders
    ):
        """The lookup only feeds the record: when it fails, validation reports what it would."""
        real = tenant_models._target_tenants

        def fail_in_a_savepoint(targets, *, instance, savepoint=False):
            if savepoint:
                msg = "replica down"
                raise DatabaseError(msg)
            return real(targets, instance=instance, savepoint=savepoint)

        item = OrderItem(tenant=tenant_a, order_id=sample_orders["b1"].pk, description="x")

        with (
            patch.object(tenant_models, "_target_tenants", side_effect=fail_in_a_savepoint),
            audit.silenced(tenant_context(tenant_a.pk)),
            pytest.raises(ValidationError) as exc,
        ):
            item.full_clean()

        assert exc.value.message_dict == {
            "order": [f"order instance with id {sample_orders['b1'].pk} is not a valid choice."],
        }
        assert records() == []

    def test_database_error_in_the_audit_lookup_leaves_the_transaction_usable(
        self, records, enforce_rls, tenant_a, sample_orders
    ):
        """The lookup runs in a savepoint: its SQL error leaves the caller's transaction usable."""

        @contextmanager
        def failing_admin_context(*, using):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
            yield  # pragma: no cover - the query raised

        item = OrderItem(tenant=tenant_a, order_id=sample_orders["b1"].pk, description="x")

        with transaction.atomic():
            with (
                patch.object(tenant_models, "admin_context", failing_admin_context),
                audit.silenced(tenant_context(tenant_a.pk)),
                pytest.raises(ValidationError) as exc,
            ):
                item.full_clean()

            assert Tenant.objects.filter(pk=tenant_a.pk).exists()  # not "transaction is aborted"

        assert exc.value.message_dict == {
            "order": [f"order instance with id {sample_orders['b1'].pk} is not a valid choice."],
        }
        assert records() == []

    def test_two_rejected_targets_are_listed_in_declaration_order(
        self, records, tenant_a, sample_order_notes, sample_order_items
    ):
        """``NoteRevision`` declares ``note`` before ``item``: not the alphabetical order."""
        revision = NoteRevision(
            tenant=tenant_a,
            note=sample_order_notes["b_linked"],
            item=sample_order_items["b1"],
            summary="x",
        )

        with pytest.raises(ValidationError):
            validate_cross_tenant_fks(revision)

        (record,) = records()
        assert record.rls_fields == ["note", "item"]

    def test_two_targets_hidden_by_rls_are_listed_in_declaration_order(
        self, records, enforce_rls, tenant_a, sample_order_notes, sample_order_items
    ):
        revision = NoteRevision(
            tenant=tenant_a,
            note_id=sample_order_notes["b_linked"].pk,
            item_id=sample_order_items["b1"].pk,
            summary="x",
        )

        with audit.silenced(tenant_context(tenant_a.pk)), pytest.raises(ValidationError):
            revision.full_clean()

        (record,) = records()
        assert record.rls_fields == ["note", "item"]

    def test_rejected_and_found_targets_are_listed_in_declaration_order(
        self, records, enforce_rls, tenant_a, sample_order_notes, sample_order_items
    ):
        """``item`` is found cross-tenant, ``note`` was rejected as not found: one record."""
        revision = NoteRevision(
            tenant=tenant_a,
            note_id=sample_order_notes["b_linked"].pk,
            item_id=sample_order_items["b1"].pk,
            summary="x",
        )
        not_found = {"note": [ValidationError("gone", code="invalid")]}

        with audit.silenced(admin_context()):
            errors = _cross_tenant_errors(revision, (), failed=not_found)

        assert list(errors) == ["item"]
        (record,) = records()
        assert record.rls_fields == ["note", "item"]

    def test_value_that_is_not_the_targets_type_logs_nothing(self, records, enforce_rls, tenant_a):
        item = OrderItem(tenant=tenant_a, order_id="abc", description="x")

        with audit.silenced(tenant_context(tenant_a.pk)), pytest.raises(ValidationError) as exc:
            item.full_clean()

        (error,) = exc.value.error_dict["order"]
        assert error.code == "invalid"  # Django's own error for "abc"
        assert "abc" in error.messages[0]
        assert records() == []

    def test_null_foreign_key_logs_nothing(self, records, enforce_rls, tenant_a):
        item = OrderItem(tenant=tenant_a, order_id=None, description="x")

        with audit.silenced(tenant_context(tenant_a.pk)), pytest.raises(ValidationError) as exc:
            item.full_clean()

        assert [error.code for error in exc.value.error_dict["order"]] == ["null"]
        assert records() == []


class TestAuditRejectedValues:
    """``_audit_rejected_values()``: the helper behind the admin's and DRF's audit records."""

    def _log(self, tenant_id, value):
        field = OrderItem._meta.get_field("order")
        _audit_rejected_values(
            OrderItem,
            tenant_id,
            {field: value},
            instance=None,
            source="admin",
            user_id=3,
        )

    def test_values_and_tenant_are_converted_with_the_fields_to_python(
        self, records, tenant_a, sample_orders
    ):
        self._log(str(tenant_a.pk), str(sample_orders["b1"].pk))

        (record,) = records()
        assert (record.rls_source, record.rls_user_id, record.rls_fields) == (
            "admin",
            3,
            ["order"],
        )
        assert record.rls_tenant_id == tenant_a.pk  # the int, not the string it came as

    def test_target_of_the_same_tenant_logs_nothing(self, records, tenant_a, sample_orders):
        self._log(str(tenant_a.pk), str(sample_orders["a1"].pk))
        assert records() == []

    @pytest.mark.parametrize("value", [None, "", "abc", [1]])
    def test_value_that_is_no_target_logs_nothing(self, records, tenant_a, value):
        self._log(tenant_a.pk, value)
        assert records() == []

    def test_tenant_that_is_not_a_tenant_id_logs_nothing(self, records, sample_orders):
        self._log("abc", sample_orders["b1"].pk)
        assert records() == []

    def test_model_without_a_tenant_field_logs_nothing(self, records, tenant_a, sample_orders):
        field = OrderItem._meta.get_field("order")
        _audit_rejected_values(
            Tag,
            tenant_a.pk,
            {field: sample_orders["b1"].pk},
            instance=None,
            source="admin",
            user_id=3,
        )
        assert records() == []

    def test_nothing_is_looked_up_below_warning(self, caplog, tenant_a, sample_orders):
        caplog.set_level(logging.ERROR, logger=audit.AUDIT_LOGGER_NAME)

        with CaptureQueriesContext(connection) as queries:
            self._log(tenant_a.pk, sample_orders["b1"].pk)

        assert len(queries) == 0


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

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/test_app/order/add/",
            "/admin/test_app/order/{pk}/change/",
            "/admin/test_app/order/{pk}/delete/",
            "/admin/test_app/order/{pk}/history/",
        ],
        ids=["add", "change", "delete", "history"],
    )
    def test_every_view_is_attributed_to_the_admin(
        self, records, enforce_rls, client, tenant_a, sample_orders, path
    ):
        user = _staff("staff_a", tenant=tenant_a)
        client.force_login(user)

        assert client.get(path.format(pk=sample_orders["a1"].pk)).status_code == 200

        admin_records = [r for r in records() if r.rls_source == "admin"]
        assert _events(admin_records) == [
            ("context.entered", "admin", tenant_a.pk, False, user.pk),
            ("context.exited", "admin", tenant_a.pk, False, user.pk),
        ]

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/test_app/order/",
            "/admin/test_app/order/add/",
            "/admin/test_app/order/{pk}/change/",
            "/admin/test_app/order/{pk}/delete/",
            "/admin/test_app/order/{pk}/history/",
        ],
        ids=["changelist", "add", "change", "delete", "history"],
    )
    def test_rls_admin_user_enters_the_admin_context(
        self, records, enforce_rls, client, sample_orders, path
    ):
        user = _staff("root", rls_admin=True)
        client.force_login(user)

        assert client.get(path.format(pk=sample_orders["a1"].pk)).status_code == 200

        admin_records = [r for r in records() if r.rls_source == "admin"]
        assert _events(admin_records) == [
            ("context.entered", "admin", None, True, user.pk),
            ("context.exited", "admin", None, True, user.pk),
        ]

    def test_switched_admin_is_attributed_to_the_chosen_tenant(
        self, records, enforce_rls, client, tenant_a, sample_orders
    ):
        user = _staff("root", rls_admin=True)
        client.force_login(user)

        client.get("/admin/test_app/order/", {"rls_tenant": str(tenant_a.pk)})

        admin_records = [r for r in records() if r.rls_source == "admin"]
        # The switcher keeps the chosen tenant in the session as a string.
        assert _events(admin_records) == [
            ("context.entered", "admin", str(tenant_a.pk), False, user.pk),
            ("context.exited", "admin", str(tenant_a.pk), False, user.pk),
        ]

    def test_tenantless_user_is_denied(self, records):
        user = SimpleNamespace(pk=8, is_tenant_admin=False, rls_tenant_id=None)
        model_admin = RLSTenantModelAdmin(Order, MagicMock())

        with pytest.raises(PermissionDenied):
            model_admin._rls_context(SimpleNamespace(user=user))

        assert [(r.rls_event, r.rls_source, r.rls_reason, r.rls_user_id) for r in records()] == [
            ("context.denied", "admin", "user_without_tenant", 8),
        ]


_ORDER_ITEM_ADD = "/admin/test_app/orderitem/add/"


def _rejected_choices(records):
    """``(source, tenant_id, user_id, model, fields)`` of each ``fk.cross_tenant_rejected``."""
    return [
        (r.rls_source, r.rls_tenant_id, r.rls_user_id, r.rls_model, r.rls_fields)
        for r in records
        if r.rls_event == "fk.cross_tenant_rejected"
    ]


def _form_error_codes(response, field):
    """The error codes the admin form reports for ``field``."""
    form = response.context["adminform"].form
    return [error.code for error in form.errors.as_data().get(field, [])]


@pytest.mark.integration
class TestAdminForeignKeyChoices:
    """The admin's scoped dropdown rejects another tenant's row as ``invalid_choice`` (#48)."""

    def test_choice_of_another_tenant_is_logged(
        self, records, enforce_rls, client, tenant_a, sample_orders
    ):
        user = _staff("staff_a", tenant=tenant_a)
        client.force_login(user)

        response = client.post(
            _ORDER_ITEM_ADD,
            {"description": "x", "order": sample_orders["b1"].pk, "_save": "Save"},
        )

        assert response.status_code == 200
        assert _form_error_codes(response, "order") == ["invalid_choice"]  # unchanged
        assert _rejected_choices(records()) == [
            ("admin", tenant_a.pk, user.pk, "test_app.OrderItem", ["order"]),
        ]

    def test_choice_of_another_tenant_by_a_switched_admin_is_logged(
        self, records, enforce_rls, client, tenant_a, sample_orders
    ):
        user = _staff("root", rls_admin=True)
        client.force_login(user)
        client.get("/admin/test_app/order/", {"rls_tenant": str(tenant_a.pk)})

        response = client.post(
            _ORDER_ITEM_ADD,
            {"description": "x", "order": sample_orders["b1"].pk, "_save": "Save"},
        )

        assert response.status_code == 200
        assert _form_error_codes(response, "order") == ["invalid_choice"]
        assert _rejected_choices(records()) == [
            ("admin", tenant_a.pk, user.pk, "test_app.OrderItem", ["order"]),
        ]

    def test_other_errors_of_the_form_do_not_hide_the_record(
        self, records, enforce_rls, client, tenant_a, sample_orders
    ):
        client.force_login(_staff("staff_a", tenant=tenant_a))

        response = client.post(
            _ORDER_ITEM_ADD,
            {"description": "", "order": sample_orders["b1"].pk, "_save": "Save"},
        )

        assert _form_error_codes(response, "description") == ["required"]
        assert [fields for *_, fields in _rejected_choices(records())] == [["order"]]

    def test_nonexistent_choice_logs_nothing(self, records, enforce_rls, client, tenant_a):
        client.force_login(_staff("staff_a", tenant=tenant_a))

        response = client.post(
            _ORDER_ITEM_ADD,
            {"description": "x", "order": 999_999, "_save": "Save"},
        )

        assert response.status_code == 200
        assert _form_error_codes(response, "order") == ["invalid_choice"]
        assert _rejected_choices(records()) == []

    def test_missing_choice_logs_nothing(self, records, enforce_rls, client, tenant_a):
        client.force_login(_staff("staff_a", tenant=tenant_a))

        response = client.post(_ORDER_ITEM_ADD, {"description": "x", "_save": "Save"})

        assert _form_error_codes(response, "order") == ["required"]
        assert _rejected_choices(records()) == []

    def test_choice_of_the_same_tenant_logs_nothing(
        self, records, enforce_rls, client, tenant_a, sample_orders
    ):
        client.force_login(_staff("staff_a", tenant=tenant_a))

        response = client.post(
            _ORDER_ITEM_ADD,
            {"description": "x", "order": sample_orders["a1"].pk, "_save": "Save"},
        )

        assert response.status_code == 302
        assert _rejected_choices(records()) == []

    def test_unbound_form_logs_nothing(self, records, enforce_rls, client, tenant_a):
        client.force_login(_staff("staff_a", tenant=tenant_a))

        assert client.get(_ORDER_ITEM_ADD).status_code == 200

        assert _rejected_choices(records()) == []

    def test_admin_without_a_tenant_chosen_is_not_rejected_by_the_dropdown(
        self, records, enforce_rls, client, tenant_a, sample_orders
    ):
        """In the admin context the dropdown shows every tenant's rows.

        The model's own check rejects the cross-tenant choice instead, and that
        record is the ``validation`` one, not the admin's.
        """
        client.force_login(_staff("root", rls_admin=True))

        response = client.post(
            _ORDER_ITEM_ADD,
            {
                "description": "x",
                "order": sample_orders["b1"].pk,
                "tenant": tenant_a.pk,
                "_save": "Save",
            },
        )

        assert _form_error_codes(response, "order") == ["invalid"]
        assert _rejected_choices(records()) == [
            ("validation", tenant_a.pk, None, "test_app.OrderItem", ["order"]),
        ]

    def test_no_record_below_warning(self, caplog, enforce_rls, client, tenant_a, sample_orders):
        caplog.set_level(logging.ERROR, logger=audit.AUDIT_LOGGER_NAME)
        client.force_login(_staff("staff_a", tenant=tenant_a))

        response = client.post(
            _ORDER_ITEM_ADD,
            {"description": "x", "order": sample_orders["b1"].pk, "_save": "Save"},
        )

        assert _form_error_codes(response, "order") == ["invalid_choice"]
        assert not [r for r in caplog.records if r.name == audit.AUDIT_LOGGER_NAME]


# ---------------------------------------------------------------------------
# Testing helpers
# ---------------------------------------------------------------------------


class TestTestingHelpers:
    """The row-count pre-check of the helpers is the library's own lookup."""

    def test_assert_rls_blocks_without_context_logs_no_context(
        self, records, enforce_rls, sample_orders
    ):
        assert_rls_blocks_without_context(Order)
        assert records() == []

    def test_aassert_rls_blocks_without_context_logs_no_context(
        self, records, enforce_rls, sample_orders
    ):
        async_to_sync(aassert_rls_blocks_without_context)(Order)
        assert records() == []


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
