"""Tests for django_rls_tenants.tenants.context."""

from __future__ import annotations

import logging

import pytest

from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc
from django_rls_tenants.tenants.context import (
    admin_context,
    tenant_context,
    with_rls_context,
)

pytestmark = pytest.mark.django_db


class TestTenantContext:
    """Tests for tenant_context()."""

    def test_sets_gucs(self, tenant_a):
        """Sets is_admin=false and current_tenant=id."""
        with tenant_context(tenant_a.pk):
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_clears_on_exit(self, tenant_a):
        """Clears GUCs after the context exits."""
        with tenant_context(tenant_a.pk):
            pass
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_restores_previous(self, tenant_a):
        """Restores previous GUC values on exit (nesting support)."""
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "999")
        with tenant_context(tenant_a.pk):
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)
        assert get_guc("rls.is_admin") == "true"
        assert get_guc("rls.current_tenant") == "999"
        clear_guc("rls.is_admin")
        clear_guc("rls.current_tenant")

    def test_none_raises_valueerror(self):
        """tenant_context(None) raises ValueError."""
        with pytest.raises(ValueError, match="tenant_id cannot be None"):  # noqa: SIM117
            with tenant_context(None):
                pass

    def test_string_tenant_id(self):
        """Works with string tenant IDs (e.g., UUIDs)."""
        with tenant_context("abc-123"):
            assert get_guc("rls.current_tenant") == "abc-123"


class TestAdminContext:
    """Tests for admin_context()."""

    def test_sets_gucs(self):
        """Sets is_admin=true and current_tenant=-1."""
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.current_tenant") == "-1"

    def test_clears_on_exit(self):
        """Clears GUCs after the context exits."""
        with admin_context():
            pass
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_restores_previous(self, tenant_a):
        """Restores previous GUC values on exit."""
        set_guc("rls.is_admin", "false")
        set_guc("rls.current_tenant", str(tenant_a.pk))
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
        assert get_guc("rls.is_admin") == "false"
        assert get_guc("rls.current_tenant") == str(tenant_a.pk)
        clear_guc("rls.is_admin")
        clear_guc("rls.current_tenant")


class TestNesting:
    """Tests for nesting tenant_context and admin_context."""

    def test_admin_inside_tenant(self, tenant_a):
        """Inner admin_context restores outer tenant_context on exit."""
        with tenant_context(tenant_a.pk):
            assert get_guc("rls.is_admin") == "false"
            with admin_context():
                assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_tenant_inside_admin(self, tenant_a):
        """Inner tenant_context restores outer admin_context on exit."""
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
            with tenant_context(tenant_a.pk):
                assert get_guc("rls.is_admin") == "false"
                assert get_guc("rls.current_tenant") == str(tenant_a.pk)
            assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.current_tenant") == "-1"

    def test_exception_in_body_cleans_up(self, tenant_a):
        """GUCs are restored even when an exception occurs in the block."""
        with pytest.raises(RuntimeError, match="boom"), tenant_context(tenant_a.pk):
            raise RuntimeError("boom")
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None


class TestWithRlsContext:
    """Tests for the @with_rls_context decorator."""

    def test_admin_user(self, admin_user):
        """Decorator sets admin context for admin user."""

        @with_rls_context
        def my_func(as_user):
            return get_guc("rls.is_admin")

        result = my_func(as_user=admin_user)
        assert result == "true"

    def test_tenant_user(self, tenant_a_user):
        """Decorator sets tenant context for tenant user."""

        @with_rls_context
        def my_func(as_user):
            return get_guc("rls.current_tenant")

        result = my_func(as_user=tenant_a_user)
        assert result == str(tenant_a_user.rls_tenant_id)

    def test_none_user_warns(self, caplog):
        """Decorator logs warning when as_user=None."""

        @with_rls_context
        def my_func(as_user):
            return "called"

        with caplog.at_level(logging.WARNING, logger="django_rls_tenants"):
            result = my_func(as_user=None)
        assert result == "called"
        assert "as_user is None" in caplog.text

    def test_none_user_no_context(self):
        """No RLS context set when as_user=None (fail-closed)."""

        @with_rls_context
        def my_func(as_user):
            return get_guc("rls.is_admin")

        result = my_func(as_user=None)
        assert result is None

    def test_positional_arg(self, tenant_a_user):
        """Works when as_user is passed as a positional argument."""

        @with_rls_context
        def my_func(as_user, extra=None):
            return get_guc("rls.current_tenant")

        result = my_func(tenant_a_user)
        assert result == str(tenant_a_user.rls_tenant_id)

    def test_keyword_arg(self, tenant_a_user):
        """Works when as_user is passed as a keyword argument."""

        @with_rls_context
        def my_func(other_arg, as_user=None):
            return get_guc("rls.current_tenant")

        result = my_func("something", as_user=tenant_a_user)
        assert result == str(tenant_a_user.rls_tenant_id)

    def test_clears_after_return(self, tenant_a_user):
        """GUCs are cleared after the decorated function returns."""

        @with_rls_context
        def my_func(as_user):
            return "done"

        my_func(as_user=tenant_a_user)
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None
