"""Tests for django_rls_tenants.tenants.checks."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.checks import Warning as CheckWarning
from django.test import override_settings

from django_rls_tenants.tenants.checks import (
    _check_conn_max_age_with_session_gucs,
    _check_guc_prefix_mismatch,
    _check_use_local_set_requires_atomic,
    check_rls_config,
)
from django_rls_tenants.tenants.conf import RLSTenantsConfig

pytestmark = pytest.mark.django_db

# Patch target for the module-level singleton used by all check functions.
_CONF_PATCH = "django_rls_tenants.tenants.conf.rls_tenants_config"


class TestCheckRlsConfig:
    """Tests for the top-level check_rls_config orchestrator."""

    def test_no_warnings_with_default_config(self):
        """Default test config produces no warnings."""
        warnings = check_rls_config()
        assert warnings == []


class TestCheckGucPrefixMismatch:
    """Tests for _check_guc_prefix_mismatch."""

    def test_no_warning_when_prefix_matches(self):
        """No warnings when constraint GUC vars match runtime config."""
        warnings = _check_guc_prefix_mismatch()
        assert warnings == []

    def test_w001_tenant_guc_mismatch(self):
        """W001 fires when GUC_PREFIX differs from RLSConstraint guc_tenant_var."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "GUC_PREFIX": "myapp",
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_guc_prefix_mismatch()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W001" in ids

    def test_w002_admin_guc_mismatch(self):
        """W002 fires when GUC_PREFIX differs from RLSConstraint guc_admin_var."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "GUC_PREFIX": "myapp",
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_guc_prefix_mismatch()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W002" in ids

    def test_both_w001_and_w002_emitted_together(self):
        """Both W001 and W002 fire for each mismatched model."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "GUC_PREFIX": "other",
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_guc_prefix_mismatch()
        ids = [w.id for w in warnings]
        # Multiple RLSProtectedModel subclasses exist in test_app
        assert ids.count("django_rls_tenants.W001") >= 1
        assert ids.count("django_rls_tenants.W002") >= 1

    def test_warnings_are_check_warning_instances(self):
        """Returned items are Django CheckWarning instances."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "GUC_PREFIX": "xxx",
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_guc_prefix_mismatch()
        assert all(isinstance(w, CheckWarning) for w in warnings)


class TestCheckUseLocalSetRequiresAtomic:
    """Tests for _check_use_local_set_requires_atomic."""

    def test_no_warning_when_use_local_set_false(self):
        """No warning when USE_LOCAL_SET=False (default)."""
        warnings = _check_use_local_set_requires_atomic()
        assert warnings == []

    def test_w003_use_local_set_without_atomic(self):
        """W003 fires when USE_LOCAL_SET=True but ATOMIC_REQUESTS is not set."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_use_local_set_requires_atomic()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W003" in ids

    def test_no_warning_when_atomic_requests_true(self):
        """No W003 when ATOMIC_REQUESTS is True alongside USE_LOCAL_SET."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "ATOMIC_REQUESTS": True,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_use_local_set_requires_atomic()
        assert warnings == []


class TestCheckConnMaxAgeWithSessionGucs:
    """Tests for _check_conn_max_age_with_session_gucs."""

    def test_no_warning_when_conn_max_age_zero(self):
        """No warning when CONN_MAX_AGE is 0 (default)."""
        warnings = _check_conn_max_age_with_session_gucs()
        assert warnings == []

    def test_no_warning_when_use_local_set_true(self):
        """No W004 when USE_LOCAL_SET=True (SET LOCAL auto-clears)."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "CONN_MAX_AGE": 600,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_conn_max_age_with_session_gucs()
        assert warnings == []

    def test_w004_conn_max_age_with_session_gucs(self):
        """W004 fires when CONN_MAX_AGE > 0 and USE_LOCAL_SET=False."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": False,
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "CONN_MAX_AGE": 600,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_conn_max_age_with_session_gucs()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W004" in ids

    def test_w004_message_includes_conn_max_age_value(self):
        """W004 message includes the actual CONN_MAX_AGE value."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": False,
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "CONN_MAX_AGE": 300,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_conn_max_age_with_session_gucs()
        assert "300" in warnings[0].msg
