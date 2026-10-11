"""Tests for django_rls_tenants.tenants.checks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.core.checks import Warning as CheckWarning
from django.db import connection
from django.test import override_settings

from django_rls_tenants.tenants.checks import (
    _check_conn_max_age_with_session_gucs,
    _check_databases_alias_exists,
    _check_databases_atomic_requests,
    _check_guc_prefix_mismatch,
    _check_superuser_connection,
    _check_tenant_fk_field_exists,
    _check_tenant_model_resolves,
    check_rls_config,
)
from django_rls_tenants.tenants.conf import RLSTenantsConfig

pytestmark = pytest.mark.django_db

# Patch target for the module-level singleton used by all check functions.
_CONF_PATCH = "django_rls_tenants.tenants.conf.rls_tenants_config"


class TestCheckRlsConfig:
    """Tests for the top-level check_rls_config orchestrator."""

    def test_no_warnings_with_default_config(self):
        """Default test config produces no warnings (excluding superuser check)."""
        warnings = check_rls_config()
        # Filter out W005 since test DB may run as superuser
        non_superuser_warnings = [w for w in warnings if w.id != "django_rls_tenants.W005"]
        assert non_superuser_warnings == []

    def test_use_local_set_without_atomic_emits_single_w007(self):
        """USE_LOCAL_SET without ATOMIC_REQUESTS yields exactly one warning (W007).

        Pins the retirement of the duplicate W003 end-to-end.
        """
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                },
                DATABASES={
                    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "test"},
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W007"]


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
        w001 = next(w for w in warnings if w.id == "django_rls_tenants.W001")
        assert "RLS_TENANTS['GUC_PREFIX']" in w001.hint

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
        assert "default" in warnings[0].msg

    def test_w004_conn_max_age_none_means_infinite(self):
        """W004 fires when CONN_MAX_AGE=None (keep connections forever)."""
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
                        "CONN_MAX_AGE": None,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_conn_max_age_with_session_gucs()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W004" in ids

    def test_w004_checks_all_configured_databases(self):
        """W004 fires for each alias with CONN_MAX_AGE > 0."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": False,
                    "DATABASES": ["default", "replica"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "CONN_MAX_AGE": 0,
                    },
                    "replica": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test_replica",
                        "CONN_MAX_AGE": 600,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_conn_max_age_with_session_gucs()
        assert len(warnings) == 1
        assert "replica" in warnings[0].msg
        assert "600" in warnings[0].msg


def _run_superuser_check_with_row(row):
    """Run ``_check_superuser_connection`` against a mocked ``pg_roles`` row."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = row
    mock_cursor.__enter__ = lambda self: self
    mock_cursor.__exit__ = MagicMock(return_value=False)
    with patch("django_rls_tenants.tenants.checks.connection") as mock_conn:
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.ops.quote_name.side_effect = connection.ops.quote_name
        warnings = _check_superuser_connection()
    return warnings, mock_cursor


def _is_superuser():
    """Return whether the live test connection's role is a superuser."""
    with connection.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        return bool(cur.fetchone()[0])


class TestCheckSuperuserConnection:
    """Tests for _check_superuser_connection (W005)."""

    def test_queries_pg_roles_for_both_attributes(self):
        """The check reads rolsuper and rolbypassrls from pg_roles."""
        _, cursor = _run_superuser_check_with_row(("app", False, False))
        sql = cursor.execute.call_args.args[0]
        assert "pg_roles" in sql
        assert "rolsuper" in sql
        assert "rolbypassrls" in sql

    def test_w005_when_superuser(self):
        """W005 fires when connected as a PostgreSQL superuser."""
        warnings, _ = _run_superuser_check_with_row(("postgres", True, False))
        assert [w.id for w in warnings] == ["django_rls_tenants.W005"]
        assert isinstance(warnings[0], CheckWarning)
        assert "is a PostgreSQL superuser" in warnings[0].msg
        assert "has the BYPASSRLS attribute" not in warnings[0].msg
        assert "'postgres'" in warnings[0].msg

    def test_w005_when_bypassrls(self):
        """W005 fires for a non-superuser role with BYPASSRLS."""
        warnings, _ = _run_superuser_check_with_row(("app", False, True))
        assert [w.id for w in warnings] == ["django_rls_tenants.W005"]
        assert "has the BYPASSRLS attribute" in warnings[0].msg
        assert "is a PostgreSQL superuser" not in warnings[0].msg
        assert "NOBYPASSRLS" in warnings[0].hint
        assert 'ALTER ROLE "app" NOSUPERUSER NOBYPASSRLS' in warnings[0].hint

    def test_w005_hint_quotes_role_identifier(self):
        """The ALTER ROLE hint quotes the role so mixed-case names stay valid SQL."""
        warnings, _ = _run_superuser_check_with_row(("App-RW", False, True))
        assert 'ALTER ROLE "App-RW" NOSUPERUSER NOBYPASSRLS' in warnings[0].hint

    def test_w005_when_superuser_and_bypassrls(self):
        """A role with both attributes yields one W005 naming both."""
        warnings, _ = _run_superuser_check_with_row(("postgres", True, True))
        assert len(warnings) == 1
        assert "is a PostgreSQL superuser and has the BYPASSRLS attribute" in warnings[0].msg

    def test_no_warning_when_plain_role(self):
        """No W005 when the role is neither superuser nor BYPASSRLS."""
        warnings, _ = _run_superuser_check_with_row(("app", False, False))
        assert warnings == []

    def test_no_warning_when_db_unavailable(self):
        """No W005 when the database is unavailable (graceful fallback)."""
        with patch(
            "django_rls_tenants.tenants.checks.connection",
        ) as mock_conn:
            mock_conn.cursor.side_effect = Exception("connection refused")
            warnings = _check_superuser_connection()
        assert warnings == []

    def test_no_warning_when_role_not_found(self):
        """No W005 when no pg_roles row comes back (defensive guard)."""
        warnings, _ = _run_superuser_check_with_row(None)
        assert warnings == []

    @pytest.mark.integration
    def test_no_w005_for_real_plain_role(self, enforce_rls):
        """Against live PostgreSQL, a NOLOGIN role (NOBYPASSRLS default) passes."""
        assert _check_superuser_connection() == []

    @pytest.mark.integration
    def test_w005_for_real_bypassrls_role(self):
        """Against live PostgreSQL, a non-superuser BYPASSRLS role triggers W005.

        ``CREATE ROLE`` is transactional, so the role disappears when the
        test transaction rolls back.
        """
        if not _is_superuser():
            pytest.skip("creating a BYPASSRLS role requires a superuser test connection")
        role = "rls_w005_bypass_role"
        with connection.cursor() as cur:
            cur.execute(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER BYPASSRLS")
            cur.execute(f"SET ROLE {role}")
        try:
            warnings = _check_superuser_connection()
        finally:
            with connection.cursor() as cur:
                cur.execute("RESET ROLE")
        assert [w.id for w in warnings] == ["django_rls_tenants.W005"]
        assert f"'{role}' has the BYPASSRLS attribute" in warnings[0].msg
        assert "superuser and" not in warnings[0].msg


class TestCheckDatabasesAliasExists:
    """Tests for _check_databases_alias_exists."""

    def test_no_warning_when_default_only(self):
        """No W006 when DATABASES contains only 'default'."""
        warnings = _check_databases_alias_exists()
        assert warnings == []

    def test_w006_invalid_alias(self):
        """W006 fires when DATABASES contains an alias not in settings.DATABASES."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default", "replca"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_alias_exists()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W006" in ids

    def test_w006_message_includes_alias(self):
        """W006 message includes the invalid alias name."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default", "replca"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_alias_exists()
        assert "replca" in warnings[0].msg

    def test_no_warning_when_all_aliases_valid(self):
        """No W006 when all DATABASES aliases exist in settings.DATABASES."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default", "replica"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                    },
                    "replica": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test_replica",
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_alias_exists()
        assert warnings == []

    def test_warnings_are_check_warning_instances(self):
        """Returned items are Django CheckWarning instances."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default", "nonexistent"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_alias_exists()
        assert all(isinstance(w, CheckWarning) for w in warnings)


class TestCheckDatabasesAtomicRequests:
    """Tests for _check_databases_atomic_requests."""

    def test_no_warning_when_use_local_set_false(self):
        """No W007 when USE_LOCAL_SET=False (default)."""
        warnings = _check_databases_atomic_requests()
        assert warnings == []

    def test_w007_use_local_set_without_atomic_on_alias(self):
        """W007 fires when USE_LOCAL_SET=True but an alias lacks ATOMIC_REQUESTS."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                    "DATABASES": ["default", "replica"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "ATOMIC_REQUESTS": True,
                    },
                    "replica": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test_replica",
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_atomic_requests()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W007" in ids
        # Only replica should trigger the warning
        assert len(warnings) == 1
        assert "replica" in warnings[0].msg

    def test_no_warning_when_all_aliases_have_atomic(self):
        """No W007 when all DATABASES aliases have ATOMIC_REQUESTS=True."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                    "DATABASES": ["default", "replica"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                        "ATOMIC_REQUESTS": True,
                    },
                    "replica": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test_replica",
                        "ATOMIC_REQUESTS": True,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_atomic_requests()
        assert warnings == []

    def test_w007_multiple_aliases_without_atomic(self):
        """W007 fires for each alias missing ATOMIC_REQUESTS."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                    "DATABASES": ["default", "replica"],
                },
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test",
                    },
                    "replica": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "test_replica",
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_atomic_requests()
        ids = [w.id for w in warnings]
        assert ids.count("django_rls_tenants.W007") == 2

    def test_no_warning_for_unmanaged_default_alias(self):
        """No warning for 'default' when it is not in RLS_TENANTS['DATABASES'].

        Regression for the retired W003, which hardcoded DATABASES['default'].
        """
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                    "DATABASES": ["tenant_db"],
                },
                DATABASES={
                    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "test"},
                    "tenant_db": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "tenant",
                        "ATOMIC_REQUESTS": True,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_databases_atomic_requests()
        assert warnings == []


class TestCheckTenantModelResolves:
    """Tests for _check_tenant_model_resolves (W008)."""

    def test_no_warning_when_model_resolves(self):
        """No W008 when TENANT_MODEL points at an installed model."""
        with (
            override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant"}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_model_resolves()
        assert warnings == []

    def test_w008_unknown_model_name(self):
        """W008 fires when the app exists but the model name does not (LookupError)."""
        with (
            override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.DoesNotExist"}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_model_resolves()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W008" in ids

    def test_w008_unknown_app_label(self):
        """W008 fires when the app label is not installed (LookupError)."""
        with (
            override_settings(RLS_TENANTS={"TENANT_MODEL": "nonexistent_app.Tenant"}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_model_resolves()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W008" in ids

    def test_w008_malformed_path(self):
        """W008 fires when TENANT_MODEL is not an 'app_label.ModelName' path (ValueError)."""
        with (
            override_settings(RLS_TENANTS={"TENANT_MODEL": "notdotted"}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_model_resolves()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W008" in ids

    def test_w008_message_includes_model_path(self):
        """W008 message includes the offending model path."""
        with (
            override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.DoesNotExist"}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_model_resolves()
        assert "test_app.DoesNotExist" in warnings[0].msg
        assert isinstance(warnings[0], CheckWarning)

    def test_no_warning_when_tenant_model_unset(self):
        """No crash and no W008 when TENANT_MODEL is missing (reported elsewhere)."""
        with (
            override_settings(RLS_TENANTS={}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_model_resolves()
        assert warnings == []


class TestCheckTenantFkFieldExists:
    """Tests for _check_tenant_fk_field_exists (W009)."""

    # The check resolves the policy field via this canonical helper; patching
    # it lets us simulate a constraint that references a missing column.
    _RESOLVER_PATCH = "django_rls_tenants.tenants.models._get_tenant_fk_field"

    def test_no_warning_with_default_config(self):
        """No W009 when every model's RLS-policy field exists (default ``tenant``)."""
        warnings = _check_tenant_fk_field_exists()
        assert warnings == []

    def test_no_warning_when_only_tenant_fk_field_changed(self):
        """Changing TENANT_FK_FIELD alone does not fire: the policy uses the constraint field.

        Regression guard against a tautological check. The ``class_prepared``
        injector always adds a field named ``TENANT_FK_FIELD``, so the policy
        filters on the constraint's ``field`` (``"tenant"`` here), which still
        exists -- no column is actually missing.
        """
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "TENANT_FK_FIELD": "nonexistent",
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_tenant_fk_field_exists()
        assert warnings == []

    def test_w009_fires_when_policy_field_missing(self):
        """W009 fires when the resolved policy field names a column no model defines."""
        with patch(self._RESOLVER_PATCH, return_value="nonexistent"):
            warnings = _check_tenant_fk_field_exists()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W009" in ids

    def test_w009_fires_once_per_protected_model(self):
        """W009 reports every concrete RLSProtectedModel subclass that lacks the field."""
        with patch(self._RESOLVER_PATCH, return_value="nonexistent"):
            warnings = _check_tenant_fk_field_exists()
        # Multiple RLSProtectedModel subclasses exist in test_app.
        assert len(warnings) >= 2
        assert all(w.id == "django_rls_tenants.W009" for w in warnings)

    def test_w009_falls_back_to_tenant_fk_field_without_constraint(self):
        """When a model has no RLSConstraint, the check falls back to TENANT_FK_FIELD."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "TENANT_FK_FIELD": "nonexistent",
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(self._RESOLVER_PATCH, return_value=None),
        ):
            warnings = _check_tenant_fk_field_exists()
        ids = [w.id for w in warnings]
        assert "django_rls_tenants.W009" in ids

    def test_w009_message_and_obj(self):
        """W009 names the missing field and binds the warning to the offending model."""
        with patch(self._RESOLVER_PATCH, return_value="nonexistent"):
            warnings = _check_tenant_fk_field_exists()
        assert "nonexistent" in warnings[0].msg
        # The actionable hint also names the exact missing field.
        assert "nonexistent" in warnings[0].hint
        assert warnings[0].obj is not None
        assert isinstance(warnings[0], CheckWarning)
