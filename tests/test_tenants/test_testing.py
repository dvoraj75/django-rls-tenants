"""Tests for django_rls_tenants.tenants.testing."""

from __future__ import annotations

import pytest

from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.testing import (
    assert_rls_blocks_without_context,
    assert_rls_enabled,
    assert_rls_policy_exists,
    rls_as_tenant,
    rls_bypass,
)
from tests.test_app.models import Order

pytestmark = pytest.mark.django_db


class TestRlsBypass:
    """Tests for rls_bypass() context manager."""

    def test_enables_admin_context(self):
        """rls_bypass sets admin GUC variables."""
        with rls_bypass():
            assert get_guc("rls.is_admin") == "true"
            # Admin mode clears tenant GUC; admin_bypass clause handles access.
            assert get_guc("rls.current_tenant") is None

    def test_clears_on_exit(self):
        """GUCs are cleared after rls_bypass exits."""
        with rls_bypass():
            pass
        assert get_guc("rls.is_admin") is None


class TestRlsAsTenant:
    """Tests for rls_as_tenant() context manager."""

    def test_scopes_to_tenant(self, tenant_a):
        """rls_as_tenant sets the correct tenant GUC."""
        with rls_as_tenant(tenant_a.pk):
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)
            assert get_guc("rls.is_admin") == "false"

    def test_clears_on_exit(self, tenant_a):
        """GUCs are cleared after rls_as_tenant exits."""
        with rls_as_tenant(tenant_a.pk):
            pass
        assert get_guc("rls.current_tenant") is None


class TestAssertRlsEnabled:
    """Tests for assert_rls_enabled()."""

    def test_passes_for_rls_table(self):
        """Passes for a table with RLS enabled and forced."""
        assert_rls_enabled("test_order")

    def test_fails_for_non_rls_table(self):
        """Fails for a table without RLS."""
        with pytest.raises(AssertionError, match="RLS is not enabled"):
            assert_rls_enabled("test_tenant")

    def test_fails_for_nonexistent_table(self):
        """Fails for a table that doesn't exist."""
        with pytest.raises(AssertionError, match="does not exist"):
            assert_rls_enabled("nonexistent_table_xyz")


class TestAssertRlsPolicyExists:
    """Tests for assert_rls_policy_exists()."""

    def test_passes_with_default_policy_name(self):
        """Passes when the default isolation policy exists."""
        assert_rls_policy_exists("test_order")

    def test_passes_with_explicit_policy_name(self):
        """Passes with an explicit policy name."""
        assert_rls_policy_exists("test_order", "test_order_tenant_isolation_policy")

    def test_fails_for_wrong_policy_name(self):
        """Fails when the specified policy doesn't exist."""
        with pytest.raises(AssertionError, match="does not exist"):
            assert_rls_policy_exists("test_order", "nonexistent_policy")


class TestAssertRlsBlocksWithoutContext:
    """Tests for assert_rls_blocks_without_context()."""

    def test_passes_when_no_rows_visible(self, enforce_rls, sample_orders):
        """Passes when RLS blocks all rows (no GUC context set)."""
        # sample_orders fixture creates data but no context is active here
        assert_rls_blocks_without_context(Order)
