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


# ---------------------------------------------------------------------------
# GUC name validation (SQL injection prevention)
# ---------------------------------------------------------------------------


class TestGucNameValidation:
    """Tests for _validate_guc_name rejecting invalid GUC names."""

    def test_sql_injection_semicolon(self):
        """GUC name with SQL injection via semicolon is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("; DROP TABLE users", "val")

    def test_sql_injection_comment(self):
        """GUC name with SQL comment injection is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls.tenant; --", "val")

    def test_empty_string(self):
        """Empty string is rejected as GUC name."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("", "val")

    def test_spaces(self):
        """GUC name with spaces is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls current_tenant", "val")

    def test_single_quotes(self):
        """GUC name with single quotes is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls'tenant", "val")

    def test_parentheses(self):
        """GUC name with parentheses is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls()", "val")

    def test_get_guc_also_validates(self):
        """get_guc rejects invalid names too."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            get_guc("; DROP TABLE users")

    def test_clear_guc_also_validates(self):
        """clear_guc rejects invalid names too."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            clear_guc("; DROP TABLE users")

    def test_valid_dotted_name_accepted(self):
        """Valid dotted GUC names like 'myapp.is_admin' are accepted."""
        set_guc("myapp.is_admin", "true")
        assert get_guc("myapp.is_admin") == "true"
        clear_guc("myapp.is_admin")

    def test_valid_underscore_start_accepted(self):
        """GUC names starting with underscore are accepted."""
        set_guc("_internal.flag", "yes")
        assert get_guc("_internal.flag") == "yes"
        clear_guc("_internal.flag")
