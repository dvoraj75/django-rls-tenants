"""Tests for django_rls_tenants.exceptions."""

from __future__ import annotations

import pytest

from django_rls_tenants.exceptions import (
    NoTenantContextError,
    RLSConfigurationError,
    RLSTenantError,
)


class TestExceptionHierarchy:
    """Verify the exception class hierarchy."""

    def test_rls_tenant_error_is_exception(self):
        """RLSTenantError inherits from Exception."""
        assert issubclass(RLSTenantError, Exception)

    def test_no_tenant_context_error_is_rls_tenant_error(self):
        """NoTenantContextError inherits from RLSTenantError."""
        assert issubclass(NoTenantContextError, RLSTenantError)

    def test_rls_configuration_error_is_rls_tenant_error(self):
        """RLSConfigurationError inherits from RLSTenantError."""
        assert issubclass(RLSConfigurationError, RLSTenantError)

    def test_no_tenant_context_error_is_not_value_error(self):
        """NoTenantContextError does not inherit from ValueError."""
        assert not issubclass(NoTenantContextError, ValueError)

    def test_rls_configuration_error_is_not_value_error(self):
        """RLSConfigurationError does not inherit from ValueError."""
        assert not issubclass(RLSConfigurationError, ValueError)

    def test_rls_tenant_error_is_not_runtime_error(self):
        """RLSTenantError does not inherit from RuntimeError."""
        assert not issubclass(RLSTenantError, RuntimeError)

    def test_catch_base_catches_no_tenant_context(self):
        """Catching RLSTenantError also catches NoTenantContextError."""
        with_caught = False
        try:
            raise NoTenantContextError("test")
        except RLSTenantError:
            with_caught = True
        assert with_caught

    def test_catch_base_catches_configuration_error(self):
        """Catching RLSTenantError also catches RLSConfigurationError."""
        with_caught = False
        try:
            raise RLSConfigurationError("test")
        except RLSTenantError:
            with_caught = True
        assert with_caught

    def test_no_tenant_context_error_message(self):
        """NoTenantContextError preserves the error message."""
        err = NoTenantContextError("missing context")
        assert str(err) == "missing context"

    def test_rls_configuration_error_message(self):
        """RLSConfigurationError preserves the error message."""
        err = RLSConfigurationError("bad config")
        assert str(err) == "bad config"


class TestErrorHints:
    """Verify the optional, keyword-only hint mechanism on RLSTenantError."""

    def test_no_hint_str_is_bare_message(self):
        """Without a hint, str(exc) is exactly the message (backward compatible)."""
        err = RLSTenantError("something broke")
        assert str(err) == "something broke"

    def test_hint_appended_to_str(self):
        """With a hint, str(exc) appends a 'Hint:' line after a blank line."""
        err = RLSTenantError("something broke", hint="turn it off and on")
        assert str(err) == "something broke\n\nHint: turn it off and on"

    def test_message_attribute_excludes_hint(self):
        """The .message attribute holds the bare message, never the hint."""
        err = RLSTenantError("bare", hint="fix it")
        assert err.message == "bare"

    def test_hint_attribute_exposes_hint(self):
        """The .hint attribute exposes the hint for programmatic access."""
        err = RLSTenantError("bare", hint="fix it")
        assert err.hint == "fix it"

    def test_hint_attribute_none_by_default(self):
        """.hint is None and .message is set when no hint is supplied."""
        err = RLSTenantError("bare")
        assert err.hint is None
        assert err.message == "bare"

    def test_hint_is_keyword_only(self):
        """hint must be passed by keyword, not positionally."""
        with pytest.raises(TypeError):
            RLSTenantError(*["bare", "fix it"])

    def test_subclass_accepts_hint(self):
        """Subclasses inherit the hint rendering and attributes."""
        err = NoTenantContextError("no context", hint="wrap it")
        assert err.message == "no context"
        assert err.hint == "wrap it"
        assert str(err) == "no context\n\nHint: wrap it"

    def test_configuration_error_accepts_hint(self):
        """RLSConfigurationError also inherits the hint behaviour."""
        err = RLSConfigurationError("bad config", hint="set TENANT_MODEL")
        assert err.hint == "set TENANT_MODEL"
        assert "Hint: set TENANT_MODEL" in str(err)

    def test_hinted_subclass_still_catchable_as_base(self):
        """A hinted subclass is still catchable as RLSTenantError with its hint intact."""
        with pytest.raises(RLSTenantError) as exc_info:
            raise NoTenantContextError("x", hint="y")
        assert exc_info.value.hint == "y"
