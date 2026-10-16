"""Tests for django_rls_tenants.tenants.conf."""

from __future__ import annotations

import warnings as w
from unittest.mock import patch

import pytest
from django.test import override_settings

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants.conf import RLSTenantsConfig, rls_tenants_config
from tests.test_app.models import Order


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

    def test_strict_mode_default(self):
        """STRICT_MODE defaults to False."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.STRICT_MODE is False

    def test_strict_mode_true(self):
        """STRICT_MODE reads True from settings."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y", "STRICT_MODE": True}):
            conf = RLSTenantsConfig()
            assert conf.STRICT_MODE is True

    def test_unique_across_tenants_default(self):
        """UNIQUE_ACROSS_TENANTS defaults to an empty dict."""
        with override_settings(RLS_TENANTS={"TENANT_MODEL": "x.Y"}):
            conf = RLSTenantsConfig()
            assert conf.UNIQUE_ACROSS_TENANTS == {}

    def test_unique_across_tenants_turns_name_lists_into_tuples(self):
        """Names stay strings; a list or tuple of field names becomes a tuple."""
        allowlist = {"billing.Company": ["slug", ["country", "vat_id"], ("a", "b")]}
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "x.Y", "UNIQUE_ACROSS_TENANTS": allowlist},
        ):
            conf = RLSTenantsConfig()
            with w.catch_warnings():
                w.simplefilter("error")  # a known key: no unknown-key warning
                assert conf.UNIQUE_ACROSS_TENANTS == {
                    "billing.Company": ["slug", ("country", "vat_id"), ("a", "b")],
                }

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            (["billing.Company"], "must be a dict"),
            ({"billing.Company": "slug"}, "each key must be a model label"),
            ({1: ["slug"]}, "each key must be a model label"),
            ({"billing.Company": [1]}, r"contains 1: an entry"),
            ({"billing.Company": [()]}, r"contains \(\): an entry"),
            ({"billing.Company": [("a", 1)]}, r"contains \('a', 1\): an entry"),
        ],
        ids=["not_a_dict", "string_value", "non_string_label", "int_entry", "empty", "mixed"],
    )
    def test_unique_across_tenants_rejects_malformed_values(self, value, match):
        """A malformed UNIQUE_ACROSS_TENANTS raises RLSConfigurationError naming the fix."""
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "x.Y", "UNIQUE_ACROSS_TENANTS": value},
        ):
            conf = RLSTenantsConfig()
            with pytest.raises(RLSConfigurationError, match=match):
                _ = conf.UNIQUE_ACROSS_TENANTS

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


class TestSettingChangedReload:
    """The module-level singleton follows ``override_settings`` (#100).

    Before 1.3.2 ``RLSTenantsConfig`` cached ``settings.RLS_TENANTS`` on first
    read and never invalidated it, so every per-test override of the setting
    was silently ignored. The app config now reloads the singleton whenever
    Django's ``setting_changed`` signal fires for ``RLS_TENANTS``.
    """

    def test_override_settings_is_seen_by_singleton(self):
        """A context-manager override takes effect and is undone on exit."""
        assert rls_tenants_config.STRICT_MODE is False
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "STRICT_MODE": True},
        ):
            assert rls_tenants_config.STRICT_MODE is True
        assert rls_tenants_config.STRICT_MODE is False

    @override_settings(
        RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True},
    )
    def test_decorator_override_is_seen_by_singleton(self):
        """The decorator form fires ``setting_changed`` on enter as well."""
        assert rls_tenants_config.USE_LOCAL_SET is True

    def test_pytest_django_settings_fixture_is_seen_by_singleton(self, settings):
        """pytest-django's ``settings`` fixture goes through the same signal."""
        settings.RLS_TENANTS = {"TENANT_MODEL": "test_app.Tenant", "GUC_PREFIX": "other"}
        assert rls_tenants_config.GUC_PREFIX == "other"
        assert rls_tenants_config.GUC_CURRENT_TENANT == "other.current_tenant"

    def test_nested_overrides_restore_each_level(self):
        """Exiting an inner override restores the outer value, not the original."""
        outer = {"TENANT_MODEL": "test_app.Tenant", "GUC_PREFIX": "outer"}
        inner = {"TENANT_MODEL": "test_app.Tenant", "GUC_PREFIX": "inner"}
        with override_settings(RLS_TENANTS=outer):
            assert rls_tenants_config.GUC_PREFIX == "outer"
            with override_settings(RLS_TENANTS=inner):
                assert rls_tenants_config.GUC_PREFIX == "inner"
            assert rls_tenants_config.GUC_PREFIX == "outer"
        assert rls_tenants_config.GUC_PREFIX == "rls"

    def test_unrelated_setting_change_keeps_cache(self):
        """Only ``RLS_TENANTS`` triggers a reload; other settings leave the cache alone."""
        _ = rls_tenants_config.TENANT_MODEL  # prime the cache
        cached = rls_tenants_config._config_cache
        assert cached is not None
        with override_settings(DEBUG=True):
            assert rls_tenants_config._config_cache is cached

    def test_app_registry_repopulation_does_not_duplicate_reload(self, settings):
        """Repeated app setup must keep one reload per settings-change signal."""
        # Isolate the unrelated handlers that ready() also connects.
        with (
            patch("django.core.signals.request_finished.connect"),
            patch("django.db.backends.signals.connection_created.connect"),
        ):
            for _ in range(2):
                with override_settings(INSTALLED_APPS=settings.INSTALLED_APPS):
                    pass

        with patch.object(rls_tenants_config, "reload", wraps=rls_tenants_config.reload) as reload:
            with override_settings(
                RLS_TENANTS={**settings.RLS_TENANTS, "STRICT_MODE": True},
            ):
                assert rls_tenants_config.STRICT_MODE is True
                assert reload.call_count == 1
            assert rls_tenants_config.STRICT_MODE is False
            assert reload.call_count == 2

    def test_unknown_key_warning_fires_again_after_override(self):
        """The typo check re-runs after a reload instead of staying silenced forever."""
        _ = rls_tenants_config.TENANT_MODEL  # first read: unknown-key check already ran
        with (
            override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "TYPO_KEY": 1}),
            w.catch_warnings(record=True) as caught,
        ):
            w.simplefilter("always")
            _ = rls_tenants_config.TENANT_MODEL
        assert any("TYPO_KEY" in str(warning.message) for warning in caught)

    def test_reload_discards_cache(self):
        """``reload()`` is the public hook the signal handler uses."""
        conf = RLSTenantsConfig()
        _ = conf.TENANT_MODEL
        assert conf._config_cache is not None
        conf.reload()
        assert conf._config_cache is None
        assert conf._unknown_keys_checked is False

    @pytest.mark.django_db
    def test_strict_mode_guard_fires_through_override_settings(self):
        """End to end: the documented testing-guide pattern now actually raises."""
        with (
            override_settings(
                RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "STRICT_MODE": True},
            ),
            pytest.raises(NoTenantContextError, match="strict mode"),
        ):
            Order.objects.count()
