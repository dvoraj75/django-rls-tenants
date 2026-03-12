"""Tests for django_rls_tenants.rls.context."""

from __future__ import annotations

import pytest

from django_rls_tenants.rls.context import bypass_flag, rls_context
from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc

pytestmark = pytest.mark.django_db


def test_rls_context_sets_and_restores():
    """rls_context sets GUCs in block and restores previous values on exit."""
    set_guc("rls.test_ctx", "original")
    with rls_context({"rls.test_ctx": "inside"}):
        assert get_guc("rls.test_ctx") == "inside"
    assert get_guc("rls.test_ctx") == "original"
    clear_guc("rls.test_ctx")


def test_rls_context_clears_when_no_previous():
    """rls_context clears GUCs on exit when no previous value existed."""
    assert get_guc("rls.test_ctx_new") is None
    with rls_context({"rls.test_ctx_new": "temp"}):
        assert get_guc("rls.test_ctx_new") == "temp"
    assert get_guc("rls.test_ctx_new") is None


def test_rls_context_nesting():
    """Inner rls_context restores outer context's values on exit."""
    with rls_context({"rls.test_nest": "outer"}):
        assert get_guc("rls.test_nest") == "outer"
        with rls_context({"rls.test_nest": "inner"}):
            assert get_guc("rls.test_nest") == "inner"
        assert get_guc("rls.test_nest") == "outer"
    assert get_guc("rls.test_nest") is None


def test_bypass_flag_sets_and_restores():
    """bypass_flag sets flag to 'true' and restores previous value."""
    set_guc("rls.test_flag", "false")
    with bypass_flag("rls.test_flag"):
        assert get_guc("rls.test_flag") == "true"
    assert get_guc("rls.test_flag") == "false"
    clear_guc("rls.test_flag")


def test_bypass_flag_clears_when_no_previous():
    """bypass_flag clears flag on exit when no previous value existed."""
    assert get_guc("rls.test_flag_new") is None
    with bypass_flag("rls.test_flag_new"):
        assert get_guc("rls.test_flag_new") == "true"
    assert get_guc("rls.test_flag_new") is None


def test_exception_in_body_still_cleans_up():
    """The finally block runs even when an exception occurs in the body."""
    assert get_guc("rls.test_exc") is None
    with pytest.raises(ValueError, match="boom"), rls_context({"rls.test_exc": "should_cleanup"}):
        raise ValueError("boom")
    assert get_guc("rls.test_exc") is None
