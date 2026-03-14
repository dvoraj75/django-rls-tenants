"""Tests for django_rls_tenants.tenants.middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from django_rls_tenants.rls.guc import clear_guc, get_guc
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware

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
        # cleanup
        clear_guc("rls.is_admin")
        clear_guc("rls.current_tenant")

    def test_admin_user_sets_gucs(self, admin_user):
        """Authenticated admin user sets admin GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=admin_user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") == "true"
        assert get_guc("rls.current_tenant") == "-1"
        clear_guc("rls.is_admin")
        clear_guc("rls.current_tenant")

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
