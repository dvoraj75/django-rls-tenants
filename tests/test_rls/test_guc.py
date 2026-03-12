"""Tests for django_rls_tenants.rls.guc."""

from __future__ import annotations

import pytest
from django.db import transaction

from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc

pytestmark = pytest.mark.django_db


def test_set_get_roundtrip():
    """set_guc then get_guc returns the value."""
    set_guc("rls.test_var", "hello")
    assert get_guc("rls.test_var") == "hello"
    clear_guc("rls.test_var")  # cleanup


def test_get_unset_returns_none():
    """get_guc for an unset variable returns None."""
    assert get_guc("rls.never_set_variable") is None


def test_clear_guc():
    """After clear_guc, get_guc returns None."""
    set_guc("rls.test_clear", "value")
    clear_guc("rls.test_clear")
    assert get_guc("rls.test_clear") is None


@pytest.mark.django_db(transaction=True)
def test_is_local_outside_transaction_raises():
    """set_guc with is_local=True outside atomic() raises RuntimeError."""
    with pytest.raises(RuntimeError, match="Cannot use SET LOCAL"):
        set_guc("rls.test_local", "value", is_local=True)


@pytest.mark.django_db(transaction=True)
def test_is_local_inside_transaction_works():
    """set_guc with is_local=True inside atomic() works and auto-clears."""
    with transaction.atomic():
        set_guc("rls.test_local_ok", "inside", is_local=True)
        assert get_guc("rls.test_local_ok") == "inside"
    # After transaction ends, SET LOCAL value is gone
    assert get_guc("rls.test_local_ok") is None


def test_session_guc_survives_multiple_queries():
    """Session-level GUC persists across multiple cursor operations."""
    set_guc("rls.test_persist", "persistent")
    # Multiple get_guc calls (each opens a new cursor)
    assert get_guc("rls.test_persist") == "persistent"
    assert get_guc("rls.test_persist") == "persistent"
    clear_guc("rls.test_persist")
