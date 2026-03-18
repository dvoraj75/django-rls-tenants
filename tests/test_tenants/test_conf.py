"""Tests for django_rls_tenants.tenants.conf."""

from __future__ import annotations

import warnings as w

import pytest
from django.test import override_settings

from django_rls_tenants.exceptions import RLSConfigurationError
from django_rls_tenants.tenants.conf import RLSTenantsConfig


class TestRLSTenantsConfig:
    """Tests for RLSTenantsConfig property accessors."""

    def test_reads_tenant_model(self):
        """Returns RLS_TENANTS["TENANT_MODEL"]."""
        conf = RLSTenantsConfig()
        assert conf.TENANT_MODEL == "test_app.Tenant"

    def test_missing_tenant_model_raises(self):
        """Missing TENANT_MODEL raises RLSConfigurationError with helpful message."""
        with override_settings(RLS_TENANTS={}):
            conf = RLSTenantsConfig()
            with pytest.raises(RLSConfigurationError, match="TENANT_MODEL"):
                _ = conf.TENANT_MODEL

    def test_missing_rls_tenants_setting_raises(self):
        """No RLS_TENANTS setting at all raises RLSConfigurationError."""
        with override_settings():
            from django.conf import settings  # noqa: PLC0415  -- must be inside override_settings

            if hasattr(settings, "RLS_TENANTS"):
                delattr(settings, "RLS_TENANTS")
            conf = RLSTenantsConfig()
            with pytest.raises(RLSConfigurationError, match="TENANT_MODEL"):
                _ = conf.TENANT_MODEL

    def test_guc_prefix_default(self):
        """GUC_PREFIX defaults to "rls"."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant"}):
            conf = RLSTenantsConfig()
            assert conf.GUC_PREFIX == "rls"

    def test_guc_prefix_custom(self):
        """GUC_PREFIX reads custom value."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y", "GUC_PREFIX": "app"}):
            conf = RLSTenantsConfig()
            assert conf.GUC_PREFIX == "app"

    def test_derived_guc_current_tenant(self):
        """GUC_CURRENT_TENANT derives from prefix."""
        conf = RLSTenantsConfig()
        assert conf.GUC_CURRENT_TENANT == "rls.current_tenant"

    def test_derived_guc_is_admin(self):
        """GUC_IS_ADMIN derives from prefix."""
        conf = RLSTenantsConfig()
        assert conf.GUC_IS_ADMIN == "rls.is_admin"

    def test_derived_gucs_with_custom_prefix(self):
        """Derived GUC names use custom prefix."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y", "GUC_PREFIX": "myapp"}):
            conf = RLSTenantsConfig()
            assert conf.GUC_CURRENT_TENANT == "myapp.current_tenant"
            assert conf.GUC_IS_ADMIN == "myapp.is_admin"

    def test_tenant_fk_field_default(self):
        """TENANT_FK_FIELD defaults to "tenant"."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.TENANT_FK_FIELD == "tenant"

    def test_user_param_name_default(self):
        """USER_PARAM_NAME defaults to "as_user"."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.USER_PARAM_NAME == "as_user"

    def test_tenant_pk_type_default(self):
        """TENANT_PK_TYPE defaults to "int"."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.TENANT_PK_TYPE == "int"

    def test_use_local_set_default(self):
        """USE_LOCAL_SET defaults to False."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.USE_LOCAL_SET is False

    def test_use_local_set_true(self):
        """USE_LOCAL_SET reads True from settings."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y", "USE_LOCAL_SET": True}):
            conf = RLSTenantsConfig()
            assert conf.USE_LOCAL_SET is True

    def test_databases_default(self):
        """DATABASES defaults to ["default"]."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.DATABASES == ["default"]

    def test_databases_custom(self):
        """DATABASES reads custom value."""
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "x.Y", "DATABASES": ["default", "replica"]},
        ):
            conf = RLSTenantsConfig()
            assert conf.DATABASES == ["default", "replica"]

    def test_unknown_key_warns(self):
        """Unrecognized keys in RLS_TENANTS emit a UserWarning."""
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "x.Y", "BOGUS_KEY": "val"},
        ):
            conf = RLSTenantsConfig()
            with w.catch_warnings(record=True) as caught:
                w.simplefilter("always")
                _ = conf.TENANT_MODEL  # triggers _warn_unknown_keys
            assert any("BOGUS_KEY" in str(warning.message) for warning in caught)
