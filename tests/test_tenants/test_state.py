"""Tests for django_rls_tenants.tenants.state."""

from __future__ import annotations

from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    reset_current_tenant_id,
    set_current_tenant_id,
)


class TestGetCurrentTenantId:
    """Tests for get_current_tenant_id()."""

    def test_default_is_none(self):
        """Returns None when no tenant context is active."""
        assert get_current_tenant_id() is None

    def test_returns_set_value(self):
        """Returns the value set by set_current_tenant_id()."""
        token = set_current_tenant_id(42)
        assert get_current_tenant_id() == 42
        reset_current_tenant_id(token)


class TestSetAndReset:
    """Tests for set_current_tenant_id() and reset_current_tenant_id()."""

    def test_set_and_get(self):
        """Set a value and retrieve it."""
        token = set_current_tenant_id(42)
        assert get_current_tenant_id() == 42
        reset_current_tenant_id(token)
        assert get_current_tenant_id() is None

    def test_reset_restores_previous(self):
        """Reset restores the value that was active before set."""
        token_outer = set_current_tenant_id(1)
        token_inner = set_current_tenant_id(2)
        assert get_current_tenant_id() == 2
        reset_current_tenant_id(token_inner)
        assert get_current_tenant_id() == 1
        reset_current_tenant_id(token_outer)
        assert get_current_tenant_id() is None

    def test_set_none_clears(self):
        """Setting None explicitly clears the state."""
        token_outer = set_current_tenant_id(42)
        token_inner = set_current_tenant_id(None)
        assert get_current_tenant_id() is None
        reset_current_tenant_id(token_inner)
        assert get_current_tenant_id() == 42
        reset_current_tenant_id(token_outer)


class TestNesting:
    """Tests for nested set/reset operations."""

    def test_three_levels(self):
        """Three levels of nesting restore correctly."""
        token_1 = set_current_tenant_id(1)
        assert get_current_tenant_id() == 1

        token_2 = set_current_tenant_id(2)
        assert get_current_tenant_id() == 2

        token_3 = set_current_tenant_id(3)
        assert get_current_tenant_id() == 3

        reset_current_tenant_id(token_3)
        assert get_current_tenant_id() == 2

        reset_current_tenant_id(token_2)
        assert get_current_tenant_id() == 1

        reset_current_tenant_id(token_1)
        assert get_current_tenant_id() is None

    def test_none_inside_value(self):
        """None (admin) inside a tenant context restores the outer value."""
        token_outer = set_current_tenant_id(42)
        token_inner = set_current_tenant_id(None)
        assert get_current_tenant_id() is None
        reset_current_tenant_id(token_inner)
        assert get_current_tenant_id() == 42
        reset_current_tenant_id(token_outer)
        assert get_current_tenant_id() is None


class TestTenantIdTypes:
    """Tests for different tenant ID types."""

    def test_integer_id(self):
        """Works with integer tenant IDs."""
        token = set_current_tenant_id(42)
        assert get_current_tenant_id() == 42
        reset_current_tenant_id(token)

    def test_string_id(self):
        """Works with string tenant IDs (e.g., UUIDs)."""
        token = set_current_tenant_id("abc-123-uuid")
        assert get_current_tenant_id() == "abc-123-uuid"
        reset_current_tenant_id(token)

    def test_string_numeric_id(self):
        """Works with string-encoded numeric IDs (from middleware GUC values)."""
        token = set_current_tenant_id("42")
        assert get_current_tenant_id() == "42"
        reset_current_tenant_id(token)
