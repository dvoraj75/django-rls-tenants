"""Tests for django_rls_tenants.tenants.context."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants.context import (
    _resolve_user_guc_vars,
    admin_context,
    tenant_context,
    with_rls_context,
)
from django_rls_tenants.tenants.state import get_current_tenant_id

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

    def test_none_raises_no_tenant_context_error(self):
        """tenant_context(None) raises NoTenantContextError."""
        with pytest.raises(NoTenantContextError, match="tenant_id cannot be None"):  # noqa: SIM117
            with tenant_context(None):
                pass

    def test_string_tenant_id(self):
        """Works with string tenant IDs (e.g., UUIDs)."""
        with tenant_context("abc-123"):
            assert get_guc("rls.current_tenant") == "abc-123"

    def test_sets_tenant_state(self, tenant_a):
        """Sets ContextVar tenant state for auto-scoping."""
        assert get_current_tenant_id() is None
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
        assert get_current_tenant_id() is None

    def test_restores_state_on_exit(self, tenant_a):
        """ContextVar state is restored on exit even after nesting."""
        with tenant_context(1):
            assert get_current_tenant_id() == 1
            with tenant_context(tenant_a.pk):
                assert get_current_tenant_id() == tenant_a.pk
            assert get_current_tenant_id() == 1
        assert get_current_tenant_id() is None

    def test_state_cleared_on_exception(self, tenant_a):
        """ContextVar state is restored when an exception occurs."""
        with pytest.raises(RuntimeError, match="boom"), tenant_context(tenant_a.pk):
            raise RuntimeError("boom")
        assert get_current_tenant_id() is None


class TestAdminContext:
    """Tests for admin_context()."""

    def test_sets_gucs(self):
        """Sets is_admin=true and clears current_tenant."""
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
            # Admin mode clears tenant GUC; admin_bypass clause handles access.
            assert get_guc("rls.current_tenant") is None

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

    def test_clears_tenant_state(self):
        """Admin context clears ContextVar tenant state (no auto-scope filter)."""
        assert get_current_tenant_id() is None
        with admin_context():
            assert get_current_tenant_id() is None
        assert get_current_tenant_id() is None

    def test_clears_state_inside_tenant(self, tenant_a):
        """Admin context inside tenant context clears and restores state."""
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
            with admin_context():
                assert get_current_tenant_id() is None
            assert get_current_tenant_id() == tenant_a.pk


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
            # Admin mode clears tenant GUC; admin_bypass handles access.
            assert get_guc("rls.current_tenant") is None

    def test_exception_in_body_cleans_up(self, tenant_a):
        """GUCs are restored even when an exception occurs in the block."""
        with pytest.raises(RuntimeError, match="boom"), tenant_context(tenant_a.pk):
            raise RuntimeError("boom")
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_state_nesting_admin_inside_tenant(self, tenant_a):
        """ContextVar state tracks nesting of admin inside tenant."""
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
            with admin_context():
                assert get_current_tenant_id() is None
            assert get_current_tenant_id() == tenant_a.pk
        assert get_current_tenant_id() is None

    def test_state_nesting_tenant_inside_admin(self, tenant_a):
        """ContextVar state tracks nesting of tenant inside admin."""
        with admin_context():
            assert get_current_tenant_id() is None
            with tenant_context(tenant_a.pk):
                assert get_current_tenant_id() == tenant_a.pk
            assert get_current_tenant_id() is None
        assert get_current_tenant_id() is None

    def test_state_nesting_tenant_inside_tenant(self, tenant_a, tenant_b):
        """ContextVar state tracks nesting of two different tenant contexts."""
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
            with tenant_context(tenant_b.pk):
                assert get_current_tenant_id() == tenant_b.pk
            assert get_current_tenant_id() == tenant_a.pk
        assert get_current_tenant_id() is None


class TestResolveUserGucVars:
    """Tests for _resolve_user_guc_vars()."""

    def test_admin_user_returns_admin_gucs(self, admin_user):
        """Admin user returns is_admin=true and empty tenant."""
        result = _resolve_user_guc_vars(admin_user)
        assert result["rls.is_admin"] == "true"
        assert result["rls.current_tenant"] == ""

    def test_tenant_user_returns_tenant_gucs(self, tenant_a_user):
        """Tenant user returns is_admin=false and tenant ID string."""
        result = _resolve_user_guc_vars(tenant_a_user)
        assert result["rls.is_admin"] == "false"
        assert result["rls.current_tenant"] == str(tenant_a_user.rls_tenant_id)

    def test_non_admin_with_none_tenant_id_raises(self):
        """Non-admin user with rls_tenant_id=None raises NoTenantContextError."""
        user = MagicMock()
        user.is_tenant_admin = False
        user.rls_tenant_id = None
        with pytest.raises(NoTenantContextError, match="rls_tenant_id=None"):
            _resolve_user_guc_vars(user)

    def test_admin_with_none_tenant_id_ok(self):
        """Admin user with rls_tenant_id=None is valid (admin bypasses RLS)."""
        user = MagicMock()
        user.is_tenant_admin = True
        user.rls_tenant_id = None
        result = _resolve_user_guc_vars(user)
        assert result["rls.is_admin"] == "true"
        assert result["rls.current_tenant"] == ""


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

    def test_non_admin_with_none_tenant_id_raises(self):
        """Decorator raises NoTenantContextError for non-admin user with rls_tenant_id=None."""
        user = MagicMock()
        user.is_tenant_admin = False
        user.rls_tenant_id = None

        @with_rls_context
        def my_func(as_user):
            return "should not reach"

        with pytest.raises(NoTenantContextError, match="rls_tenant_id=None"):
            my_func(as_user=user)


class TestIsLocalMode:
    """Tests for USE_LOCAL_SET=True (transaction-scoped GUCs)."""

    @pytest.mark.django_db(transaction=True)
    def test_tenant_context_is_local(self, tenant_a):
        """tenant_context with USE_LOCAL_SET=True auto-clears on commit."""
        from django.db import transaction  # noqa: PLC0415
        from django.test import override_settings  # noqa: PLC0415

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            }
        ):
            # Reset config cache so override is picked up
            from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

            rls_tenants_config._config_cache = None
            rls_tenants_config._unknown_keys_checked = False
            try:
                with transaction.atomic(), tenant_context(tenant_a.pk):
                    assert get_guc("rls.is_admin") == "false"
                    assert get_guc("rls.current_tenant") == str(tenant_a.pk)
                # After transaction, SET LOCAL values are gone
                assert get_guc("rls.is_admin") is None
                assert get_guc("rls.current_tenant") is None
            finally:
                rls_tenants_config._config_cache = None
                rls_tenants_config._unknown_keys_checked = False

    @pytest.mark.django_db(transaction=True)
    def test_admin_context_is_local(self):
        """admin_context with USE_LOCAL_SET=True auto-clears on commit."""
        from django.db import transaction  # noqa: PLC0415
        from django.test import override_settings  # noqa: PLC0415

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            }
        ):
            from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

            rls_tenants_config._config_cache = None
            rls_tenants_config._unknown_keys_checked = False
            try:
                with transaction.atomic(), admin_context():
                    assert get_guc("rls.is_admin") == "true"
                # After transaction, SET LOCAL values are gone
                assert get_guc("rls.is_admin") is None
            finally:
                rls_tenants_config._config_cache = None
                rls_tenants_config._unknown_keys_checked = False
