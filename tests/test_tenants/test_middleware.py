"""Tests for django_rls_tenants.tenants.middleware."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants.middleware import (
    RLSTenantMiddleware,
    _clear_gucs_set_flag,
    _were_gucs_set,
)
from django_rls_tenants.tenants.state import get_current_tenant_id

pytestmark = pytest.mark.django_db


def _make_request(user=None):
    """Create a mock HttpRequest with optional user."""
    request = MagicMock()
    if user is not None:
        request.user = user
    else:
        del request.user  # hasattr(request, 'user') will be False
    return request


class TestProcessRequest:
    """Tests for RLSTenantMiddleware.process_request()."""

    def test_tenant_user_sets_gucs(self, tenant_a_user):
        """Authenticated tenant user sets correct GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") == "false"
        assert get_guc("rls.current_tenant") == str(tenant_a_user.rls_tenant_id)

    def test_admin_user_sets_gucs(self, admin_user):
        """Authenticated admin user sets admin GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=admin_user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") == "true"
        # Admin mode clears tenant GUC; admin_bypass clause handles access.
        assert get_guc("rls.current_tenant") is None

    def test_unauthenticated_no_gucs(self):
        """Unauthenticated request sets no GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = False
        request = _make_request(user=user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_no_user_attribute_no_gucs(self):
        """Request without user attribute sets no GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=None)  # no .user attribute
        mw.process_request(request)
        assert get_guc("rls.is_admin") is None


class TestProcessResponse:
    """Tests for RLSTenantMiddleware.process_response()."""

    def test_clears_gucs(self, tenant_a_user):
        """process_response clears GUCs after request processing."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        # GUCs are now set
        assert get_guc("rls.current_tenant") is not None
        # process_response should clear them
        response = MagicMock()
        mw.process_response(request, response)
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_returns_response(self, tenant_a_user):
        """process_response returns the response object."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        response = MagicMock()
        result = mw.process_response(request, response)
        assert result is response

    def test_skips_clear_when_use_local_set_true(self, tenant_a_user):
        """process_response skips clearing GUCs when USE_LOCAL_SET=True."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_guc("rls.current_tenant") is not None

        with patch("django_rls_tenants.tenants.middleware.rls_tenants_config") as mock_conf:
            mock_conf.USE_LOCAL_SET = True
            response = MagicMock()
            mw.process_response(request, response)

        # GUCs should still be set (SET LOCAL auto-clears at transaction end)
        assert get_guc("rls.current_tenant") is not None


class TestThreadLocalFlags:
    """Tests for thread-local GUC flag helpers."""

    def test_were_gucs_set_false_by_default(self):
        """_were_gucs_set() returns False when no flag has been set."""
        _clear_gucs_set_flag()
        assert _were_gucs_set() is False

    def test_mark_and_check(self, tenant_a_user):
        """_mark_gucs_set marks the flag, _were_gucs_set reads it."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        _clear_gucs_set_flag()
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert _were_gucs_set() is True


class TestAutoScopeState:
    """Tests for ContextVar tenant state management in middleware."""

    def test_sets_state_for_tenant_user(self, tenant_a_user):
        """Middleware sets ContextVar state for tenant user."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_current_tenant_id() == str(tenant_a_user.rls_tenant_id)

    def test_clears_state_for_admin_user(self, admin_user):
        """Middleware sets ContextVar state to None for admin user."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=admin_user)
        mw.process_request(request)
        assert get_current_tenant_id() is None

    def test_no_state_for_unauthenticated(self):
        """Middleware does not set ContextVar state for unauthenticated request."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = False
        request = _make_request(user=user)
        mw.process_request(request)
        assert get_current_tenant_id() is None

    def test_process_response_clears_state(self, tenant_a_user):
        """process_response clears ContextVar state."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_current_tenant_id() is not None
        response = MagicMock()
        mw.process_response(request, response)
        assert get_current_tenant_id() is None

    def test_full_request_response_cycle(self, tenant_a_user):
        """Full request/response cycle sets and clears ContextVar state."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)

        # Before request
        assert get_current_tenant_id() is None

        # After process_request
        mw.process_request(request)
        assert get_current_tenant_id() == str(tenant_a_user.rls_tenant_id)

        # After process_response
        mw.process_response(request, MagicMock())
        assert get_current_tenant_id() is None


class TestProcessRequestExceptionHandling:
    """Tests for error handling in process_request."""

    def test_clears_gucs_and_reraises_on_resolve_error(self):
        """When _resolve_user_guc_vars raises, both GUCs and state are cleared."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = True
        request = _make_request(user=user)

        # Pre-set a GUC to verify cleanup
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "99")

        with (
            patch(
                "django_rls_tenants.tenants.middleware._resolve_user_guc_vars",
                side_effect=RuntimeError("bad user"),
            ),
            pytest.raises(RuntimeError, match="bad user"),
        ):
            mw.process_request(request)

        # Both GUCs should have been cleared as safety measure
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None
        # ContextVar state should also be cleared
        assert get_current_tenant_id() is None

    def test_survives_double_failure_on_cleanup(self):
        """When both set_guc and cleanup clear_guc fail, the original error propagates."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = True
        request = _make_request(user=user)

        with (
            patch(
                "django_rls_tenants.tenants.middleware._resolve_user_guc_vars",
                side_effect=RuntimeError("connection lost"),
            ),
            patch(
                "django_rls_tenants.tenants.middleware.clear_guc",
                side_effect=RuntimeError("cleanup also failed"),
            ),
            pytest.raises(RuntimeError, match="connection lost"),
        ):
            mw.process_request(request)
