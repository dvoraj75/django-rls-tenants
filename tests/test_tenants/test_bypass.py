"""Tests for django_rls_tenants.tenants.bypass."""

from __future__ import annotations

import inspect

import pytest
from django.db import transaction

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

    @pytest.mark.django_db(transaction=True)
    def test_clear_bypass_flag_is_local_inside_atomic(self):
        """clear_bypass_flag(is_local=True) clears a transaction-scoped flag."""
        with transaction.atomic():
            set_bypass_flag("rls.test_local_clear", is_local=True)
            assert get_guc("rls.test_local_clear") == "true"
            clear_bypass_flag("rls.test_local_clear", is_local=True)
            assert get_guc("rls.test_local_clear") is None

    @pytest.mark.django_db(transaction=True)
    def test_clear_bypass_flag_is_local_outside_atomic_raises(self):
        """clear_bypass_flag(is_local=True) outside atomic() raises RuntimeError."""
        with pytest.raises(RuntimeError, match="Cannot use SET LOCAL"):
            clear_bypass_flag("rls.test_local_clear", is_local=True)

    def test_set_and_clear_share_keyword_signature(self):
        """set_bypass_flag and clear_bypass_flag accept the same keyword-only params."""
        set_params = inspect.signature(set_bypass_flag).parameters
        clear_params = inspect.signature(clear_bypass_flag).parameters
        assert set(set_params) == set(clear_params) == {"flag_name", "is_local", "using"}
