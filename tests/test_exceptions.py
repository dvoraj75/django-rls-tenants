"""Tests for django_rls_tenants.exceptions."""

from __future__ import annotations

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
