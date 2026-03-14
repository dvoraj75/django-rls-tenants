"""Tests for django_rls_tenants.tenants.bypass."""

from __future__ import annotations

import pytest

from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.bypass import (
    bypass_flag,
    clear_bypass_flag,
    set_bypass_flag,
)

pytestmark = pytest.mark.django_db


class TestBypassFlagContextManager:
    """Tests for bypass_flag() context manager."""

    def test_sets_and_clears(self):
        """bypass_flag sets flag to 'true' and clears on exit."""
        with bypass_flag("rls.test_bypass"):
            assert get_guc("rls.test_bypass") == "true"
        assert get_guc("rls.test_bypass") is None

    def test_restores_previous(self):
        """bypass_flag restores previous value on exit."""
        set_bypass_flag("rls.test_bypass")
        assert get_guc("rls.test_bypass") == "true"
        # Nesting: bypass_flag should save "true" and restore it
        with bypass_flag("rls.test_bypass"):
            assert get_guc("rls.test_bypass") == "true"
        assert get_guc("rls.test_bypass") == "true"
        clear_bypass_flag("rls.test_bypass")


class TestImperativeBypass:
    """Tests for set_bypass_flag / clear_bypass_flag."""

    def test_set_bypass_flag(self):
        """set_bypass_flag sets the flag to 'true'."""
        set_bypass_flag("rls.test_imperative")
        assert get_guc("rls.test_imperative") == "true"
        clear_bypass_flag("rls.test_imperative")

    def test_clear_bypass_flag(self):
        """clear_bypass_flag clears the flag."""
        set_bypass_flag("rls.test_imperative_clear")
        clear_bypass_flag("rls.test_imperative_clear")
        assert get_guc("rls.test_imperative_clear") is None

    def test_clear_unset_flag_no_error(self):
        """Clearing a flag that was never set does not error."""
        clear_bypass_flag("rls.never_set_flag")
        assert get_guc("rls.never_set_flag") is None
