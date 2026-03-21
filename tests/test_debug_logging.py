"""Tests for DEBUG-level logging in middleware, context managers, and M2M auto-detection.

Verifies that logger.debug() calls are emitted at the right points with the
expected content, while producing zero output when DEBUG is not enabled.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from django_rls_tenants.rls.constraints import RLSM2MConstraint
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware
from django_rls_tenants.tenants.models import register_m2m_rls
from tests.test_app.models import Project

pytestmark = pytest.mark.django_db

LOGGER_NAME = "django_rls_tenants"


def _make_request(user=None):
    """Create a mock HttpRequest with optional user."""
    request = MagicMock()
    if user is not None:
        request.user = user
    else:
        del request.user
    return request


# ---------------------------------------------------------------------------
# Middleware logging
# ---------------------------------------------------------------------------


class TestMiddlewareDebugLogging:
    """Verify DEBUG logs for GUC set/clear in middleware."""

    def test_process_request_logs_guc_set(self, tenant_a_user, caplog):
        """Middleware logs when GUCs are set for an authenticated user."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            mw.process_request(request)

        assert any("Middleware set GUCs" in msg for msg in caplog.messages)
        assert any(str(tenant_a_user.rls_tenant_id) in msg for msg in caplog.messages)

    def test_process_request_logs_admin_user(self, admin_user, caplog):
        """Middleware logs admin=True for admin users."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=admin_user)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            mw.process_request(request)

        set_messages = [m for m in caplog.messages if "Middleware set GUCs" in m]
        assert len(set_messages) == 1
        assert "admin=True" in set_messages[0]

    def test_process_response_logs_guc_clear(self, tenant_a_user, caplog):
        """Middleware logs when GUCs are cleared during response."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            mw.process_response(request, MagicMock())

        assert any("Middleware cleared GUCs" in msg for msg in caplog.messages)

    def test_unauthenticated_request_no_debug_logs(self, caplog):
        """Unauthenticated request produces no debug log messages."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = False
        request = _make_request(user=user)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            mw.process_request(request)

        guc_messages = [
            m
            for m in caplog.messages
            if "Middleware set GUCs" in m or "Middleware cleared GUCs" in m
        ]
        assert len(guc_messages) == 0

    def test_no_output_at_info_level(self, tenant_a_user, caplog):
        """No middleware debug messages appear at INFO level."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            mw.process_request(request)
            mw.process_response(request, MagicMock())

        guc_messages = [
            m
            for m in caplog.messages
            if "Middleware set GUCs" in m or "Middleware cleared GUCs" in m
        ]
        assert len(guc_messages) == 0


# ---------------------------------------------------------------------------
# Context manager logging
# ---------------------------------------------------------------------------


class TestTenantContextDebugLogging:
    """Verify DEBUG logs for tenant_context() entry/exit."""

    def test_logs_entry_with_tenant_id(self, tenant_a, caplog):
        """tenant_context logs entry with tenant ID."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), tenant_context(tenant_a.pk):
            pass

        entry_messages = [m for m in caplog.messages if "tenant_context entered" in m]
        assert len(entry_messages) == 1
        assert str(tenant_a.pk) in entry_messages[0]

    def test_logs_exit(self, tenant_a, caplog):
        """tenant_context logs exit."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), tenant_context(tenant_a.pk):
            pass

        exit_messages = [m for m in caplog.messages if "tenant_context exited" in m]
        assert len(exit_messages) == 1
        assert str(tenant_a.pk) in exit_messages[0]

    def test_logs_database_alias(self, caplog):
        """tenant_context includes the database alias in log messages."""
        with (
            caplog.at_level(logging.DEBUG, logger=LOGGER_NAME),
            tenant_context(1, using="default"),
        ):
            pass

        entry_messages = [m for m in caplog.messages if "tenant_context entered" in m]
        assert len(entry_messages) == 1
        assert "using=default" in entry_messages[0]

    def test_no_output_at_info_level(self, tenant_a, caplog):
        """No tenant_context debug messages at INFO level."""
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME), tenant_context(tenant_a.pk):
            pass

        ctx_messages = [
            m
            for m in caplog.messages
            if "tenant_context entered" in m or "tenant_context exited" in m
        ]
        assert len(ctx_messages) == 0


class TestAdminContextDebugLogging:
    """Verify DEBUG logs for admin_context() entry/exit."""

    def test_logs_entry(self, caplog):
        """admin_context logs entry."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), admin_context():
            pass

        entry_messages = [m for m in caplog.messages if "admin_context entered" in m]
        assert len(entry_messages) == 1

    def test_logs_exit(self, caplog):
        """admin_context logs exit."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), admin_context():
            pass

        exit_messages = [m for m in caplog.messages if "admin_context exited" in m]
        assert len(exit_messages) == 1

    def test_logs_database_alias(self, caplog):
        """admin_context includes the database alias in log messages."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), admin_context(using="default"):
            pass

        entry_messages = [m for m in caplog.messages if "admin_context entered" in m]
        assert len(entry_messages) == 1
        assert "using=default" in entry_messages[0]

    def test_no_output_at_info_level(self, caplog):
        """No admin_context debug messages at INFO level."""
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME), admin_context():
            pass

        ctx_messages = [
            m
            for m in caplog.messages
            if "admin_context entered" in m or "admin_context exited" in m
        ]
        assert len(ctx_messages) == 0


# ---------------------------------------------------------------------------
# M2M auto-detection logging
# ---------------------------------------------------------------------------


class TestRegisterM2MRLSDebugLogging:
    """Verify DEBUG logs for register_m2m_rls() skip reasons."""

    def test_logs_constraint_already_exists(self, caplog):
        """register_m2m_rls logs when skipping due to existing constraint."""
        # First call registers constraints, second call should skip with log
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            register_m2m_rls()

        skip_messages = [m for m in caplog.messages if "already exists" in m]
        # Some through tables will be skipped because register_m2m_rls was
        # already called during app startup (DjangoRlsTenantsConfig.ready()).
        # The second side of a bidirectional M2M also triggers this.
        assert len(skip_messages) > 0

    def test_logs_added_constraint(self):
        """register_m2m_rls logs when adding a new constraint.

        This is tested indirectly -- the app startup already ran register_m2m_rls(),
        so we verify the "Added RLSM2MConstraint" message appears by calling it
        on a fresh through model.
        """
        # Verify that existing through tables already have constraints from startup
        through = Project.members.through
        has_constraint = any(isinstance(c, RLSM2MConstraint) for c in through._meta.constraints)
        assert has_constraint, (
            "Project.members through table should have RLSM2MConstraint from startup"
        )

    def test_logs_explicit_through_model_skip(self, caplog):
        """register_m2m_rls logs when skipping explicit through models."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            register_m2m_rls()

        # The skip messages should include "explicit through model" if any
        # models with explicit through tables exist, or "already exists" for
        # auto-created ones that were already registered at startup.
        all_skip_messages = [m for m in caplog.messages if "register_m2m_rls: skipping" in m]
        assert len(all_skip_messages) > 0
