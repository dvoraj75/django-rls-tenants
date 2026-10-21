"""Tests for django_rls_tenants.tenants.checks."""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
from django.apps import apps
from django.core.checks import Error as CheckError
from django.core.checks import Warning as CheckWarning
from django.core.exceptions import ImproperlyConfigured
from django.db import InterfaceError, OperationalError, ProgrammingError, connection, models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.test import override_settings
from django.test.utils import isolate_apps

from django_rls_tenants.pool import _policy_guc_names, reset_rls_gucs
from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.rls.predicates import RawPredicate, SharedWith, TenantField
from django_rls_tenants.tenants.checks import (
    _check_conn_max_age_with_session_gucs,
    _check_databases_alias_exists,
    _check_external_models,
    _check_extra_predicates,
    _check_guc_prefix_mismatch,
    _check_policy_guc_defaults,
    _check_pool_without_reset,
    _check_protected_models_have_constraint,
    _check_strict_mode_exclude,
    _check_superuser_connection,
    _check_tenant_fk_field_exists,
    _check_tenant_model_resolves,
    _check_unique_across_tenants,
    _checked_aliases,
    check_rls_config,
)
from django_rls_tenants.tenants.conf import RLSTenantsConfig
from django_rls_tenants.tenants.models import RLSProtectedModel
from tests.test_app.models import AdminUser, Tenant

pytestmark = pytest.mark.django_db

# Patch target for the module-level singleton used by all check functions.
_CONF_PATCH = "django_rls_tenants.tenants.conf.rls_tenants_config"

# The test app's OrderShipment.order is a one-to-one field (#44), unique across
# tenants by nature: a config of a test's own that runs the checks over the real
# registry carries the UNIQUE_ACROSS_TENANTS entry tests/settings.py has, or W012
# reports it.
_TEST_APP_ALLOWLIST = {"test_app.OrderShipment": ["order"]}


class TestCheckRlsConfig:
    """Tests for the top-level check_rls_config orchestrator."""

    def test_no_warnings_with_default_config(self):
        """Default test config produces no warnings (excluding superuser check)."""
        warnings = check_rls_config()
        # Filter out W005 / E001 since the test DB may run as a superuser
        bypass_ids = {"django_rls_tenants.W005", "django_rls_tenants.E001"}
        assert [w for w in warnings if w.id not in bypass_ids] == []

    def test_use_local_set_without_atomic_emits_nothing(self):
        """USE_LOCAL_SET without ATOMIC_REQUESTS is a valid setup (W003/W007 retired).

        Since 1.3.2 ``RLSTenantMiddleware`` opens its own transaction when
        ``USE_LOCAL_SET=True``, so ``ATOMIC_REQUESTS`` is no longer required
        and neither W003 (retired 1.3.1) nor W007 (retired 1.3.2) may fire.
        """
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "USE_LOCAL_SET": True,
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
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
        assert warnings == []

    # Dropping any of these helpers from ``check_rls_config`` must fail the
    # aggregation test, even when the real helper would return no warnings.
    _HELPERS = (
        "_check_superuser_connection",
        "_check_policy_guc_defaults",
        "_check_guc_prefix_mismatch",
        "_check_conn_max_age_with_session_gucs",
        "_check_pool_without_reset",
        "_check_databases_alias_exists",
        "_check_tenant_model_resolves",
        "_check_tenant_fk_field_exists",
        "_check_unique_across_tenants",
        "_check_protected_models_have_constraint",
        "_check_strict_mode_exclude",
        "_check_external_models",
        "_check_extra_predicates",
    )

    def test_aggregates_every_helper(self):
        """Each ``_check_*`` helper's warnings reach the aggregated result, in order."""
        patches = [
            patch(
                f"django_rls_tenants.tenants.checks.{name}",
                return_value=[CheckWarning(name, id=f"django_rls_tenants.T{index:03d}")],
            )
            for index, name in enumerate(self._HELPERS)
        ]
        with ExitStack() as stack:
            for helper_patch in patches:
                stack.enter_context(helper_patch)
            warnings = check_rls_config()
        assert [w.id for w in warnings] == [
            f"django_rls_tenants.T{index:03d}" for index in range(len(self._HELPERS))
        ]

    def test_w008_surfaces_from_real_config(self):
        """An unresolvable TENANT_MODEL reaches the aggregated result as W008."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "nope.Nope",
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W008"]

    def test_w009_surfaces_from_orchestrator(self):
        """A policy field no model defines reaches the aggregated result as W009."""
        with (
            patch(
                "django_rls_tenants.tenants.models._get_tenant_fk_field",
                return_value="nonexistent",
            ),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        ids = {w.id for w in warnings}
        assert ids == {"django_rls_tenants.W009"}
        assert len(warnings) >= 2  # one per concrete RLSProtectedModel subclass

    def test_w012_surfaces_from_real_config(self):
        """A stale UNIQUE_ACROSS_TENANTS label reaches the aggregated result as W012."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "UNIQUE_ACROSS_TENANTS": {"test_app.Nope": ["slug"], **_TEST_APP_ALLOWLIST},
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W012"]

    def test_w015_surfaces_from_real_config(self):
        """A STRICT_MODE_EXCLUDE label that names no model reaches the result as W015."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "STRICT_MODE_EXCLUDE": ["test_app.Order", "test_app.Nope"],
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W015"]

    def test_w010_surfaces_from_real_config(self):
        """A pooled alias without a reset reaches the aggregated result as W010."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
                },
                DATABASES={"default": _pooled_database({"max_size": 4})},
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W010"]

    def test_w006_surfaces_from_real_config(self):
        """An unknown alias in RLS_TENANTS['DATABASES'] reaches the result as W006."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default", "nope"],
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W006"]


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

    def test_reports_a_model_with_a_constraint_but_not_the_base_class(self):
        """W001/W002 cover a model protected by its ``RLSConstraint`` alone (#159).

        ``Label`` has the constraint without inheriting ``RLSProtectedModel``;
        its policy reads the default GUC names, so a changed prefix breaks its
        isolation like any other model's.
        """
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
        by_model = {(w.id, w.msg.split(" ")[2]) for w in warnings}
        assert ("django_rls_tenants.W001", "Label") in by_model
        assert ("django_rls_tenants.W002", "Label") in by_model
        # A proxy has no constraints of its own: its concrete model is reported.
        assert not any(name == "OrderProxy" for _, name in by_model)


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

    def test_w004_no_longer_recommends_atomic_requests(self):
        """W004 points at USE_LOCAL_SET alone; ATOMIC_REQUESTS is not a requirement (#95)."""
        with (
            override_settings(
                RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant"},
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
        assert len(warnings) == 1
        assert "ATOMIC_REQUESTS" not in warnings[0].msg
        assert "ATOMIC_REQUESTS" not in str(warnings[0].hint)
        assert "USE_LOCAL_SET'] = True" in str(warnings[0].hint)


def _pooled_database(pool):
    """A ``settings.DATABASES`` entry with ``OPTIONS["pool"] = pool``."""
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "test",
        "OPTIONS": {"pool": pool},
    }


def _run_pool_check(databases, **rls_tenants):
    with (
        override_settings(
            RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", **rls_tenants},
            DATABASES=databases,
        ),
        patch(_CONF_PATCH, RLSTenantsConfig()),
    ):
        return _check_pool_without_reset()


def _reset(conn):
    """A pool ``reset`` callback that is not the library's own."""


class TestCheckPoolWithoutReset:
    """Tests for _check_pool_without_reset (W010, #96)."""

    @pytest.mark.parametrize(
        "pool",
        [True, {"max_size": 4}, {"max_size": 4, "reset": None}],
        ids=["true", "options", "reset-none"],
    )
    def test_w010_pool_without_reset(self, pool):
        warnings = _run_pool_check({"default": _pooled_database(pool)})

        assert [w.id for w in warnings] == ["django_rls_tenants.W010"]
        assert "DATABASES['default']" in warnings[0].msg
        assert "reset_rls_gucs" in str(warnings[0].hint)
        assert "USE_LOCAL_SET'] = True" in str(warnings[0].hint)

    @pytest.mark.parametrize("reset", [reset_rls_gucs, _reset], ids=["library", "custom"])
    def test_no_warning_with_a_reset(self, reset):
        """A reset of the user's own is not second-guessed."""
        assert _run_pool_check({"default": _pooled_database({"reset": reset})}) == []

    @pytest.mark.parametrize("pool", [False, None, {}], ids=["false", "none", "empty-dict"])
    def test_no_warning_without_a_pool(self, pool):
        """Django treats a falsy ``OPTIONS["pool"]``, including ``{}``, as no pool."""
        assert _run_pool_check({"default": _pooled_database(pool)}) == []

    def test_no_warning_with_use_local_set(self):
        warnings = _run_pool_check({"default": _pooled_database(True)}, USE_LOCAL_SET=True)

        assert warnings == []

    def test_checks_only_configured_aliases(self):
        databases = {
            "default": _pooled_database({"reset": _reset}),
            "replica": _pooled_database(True),
            "analytics": _pooled_database(True),
        }

        warnings = _run_pool_check(databases, DATABASES=["default", "replica"])

        assert [w.id for w in warnings] == ["django_rls_tenants.W010"]
        assert "DATABASES['replica']" in warnings[0].msg

    def test_unknown_alias_is_left_to_w006(self):
        warnings = _run_pool_check(
            {"default": _pooled_database({"reset": _reset})},
            DATABASES=["default", "nope"],
        )

        assert warnings == []


def _fake_connection(
    rows=(),
    *,
    connect_error=None,
    query_error=None,
    vendor="postgresql",
    savepoint=None,
):
    """Return a mocked connection whose catalogue query returns ``rows``, and its cursor.

    ``connect_error`` is raised by ``cursor()`` (opening the connection),
    ``query_error`` by ``execute()`` (the query itself). ``savepoint`` is what
    ``connection.savepoint()`` returns: ``None`` outside a transaction (Django's
    behaviour in autocommit), a savepoint id inside one.
    """
    cursor = MagicMock()
    cursor.fetchall.return_value = list(rows)
    cursor.__enter__ = lambda self: self
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.execute.side_effect = query_error
    conn = MagicMock()
    conn.vendor = vendor
    conn.savepoint.return_value = savepoint
    conn.ops.quote_name.side_effect = connection.ops.quote_name
    if connect_error is None:
        conn.cursor.return_value = cursor
    else:
        conn.cursor.side_effect = connect_error
    return conn, cursor


@contextmanager
def _fake_aliases(**conns):
    """Make the database checks see ``conns`` (alias -> mocked connection) only."""
    with (
        patch("django_rls_tenants.tenants.checks.connections", conns),
        patch(
            "django_rls_tenants.tenants.checks._checked_aliases",
            return_value=list(conns),
        ),
    ):
        yield


def _run_superuser_check_with_row(row, *, debug=True):
    """Run ``_check_superuser_connection`` against a mocked ``pg_roles`` row."""
    conn, cursor = _fake_connection([] if row is None else [row])
    with _fake_aliases(default=conn), override_settings(DEBUG=debug):
        warnings = _check_superuser_connection()
    return warnings, cursor


def _is_superuser():
    """Return whether the live test connection's role is a superuser."""
    with connection.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        return bool(cur.fetchone()[0])


class TestCheckedAliases:
    """Tests for _checked_aliases, the aliases W005, E001, W011 and W013 query."""

    def test_default_only(self):
        assert _checked_aliases() == ["default"]

    def test_every_configured_alias_in_order(self):
        databases = {"default": {}, "replica": {}, "other": {}}
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["replica", "default"],
                },
                DATABASES=databases,
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            assert _checked_aliases() == ["replica", "default"]

    def test_unknown_alias_is_left_to_w006(self):
        with (
            override_settings(
                RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "DATABASES": ["default", "nope"]}
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            assert _checked_aliases() == ["default"]


class TestCheckSuperuserConnection:
    """Tests for _check_superuser_connection (W005, E001, W013)."""

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
        assert "DATABASES['default']" in warnings[0].msg

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

    @pytest.mark.parametrize("row", [("postgres", True, False), ("app", False, True)])
    def test_e001_instead_of_w005_without_debug(self, row):
        """With DEBUG off a bypassing role is the E001 error, not W005 (#108)."""
        warnings, _ = _run_superuser_check_with_row(row, debug=False)
        assert [w.id for w in warnings] == ["django_rls_tenants.E001"]
        assert isinstance(warnings[0], CheckError)
        assert warnings[0].is_serious()
        assert f"{row[0]!r} of DATABASES['default']" in warnings[0].msg
        assert "settings.DEBUG is False" in warnings[0].msg
        assert "NOSUPERUSER NOBYPASSRLS" in warnings[0].hint
        assert "SILENCED_SYSTEM_CHECKS = ['django_rls_tenants.E001']" in warnings[0].hint

    @pytest.mark.parametrize("debug", [True, False])
    def test_nothing_for_a_plain_role(self, debug):
        """Neither W005 nor E001 when the role is neither superuser nor BYPASSRLS."""
        warnings, _ = _run_superuser_check_with_row(("app", False, False), debug=debug)
        assert warnings == []

    def test_nothing_when_role_not_found(self):
        """No W005 when no pg_roles row comes back (defensive guard)."""
        warnings, _ = _run_superuser_check_with_row(None)
        assert warnings == []

    def test_every_configured_alias(self):
        """A replica connected as a superuser is reported under its own alias."""
        default, _ = _fake_connection([("app", False, False)])
        replica, _ = _fake_connection([("replica_admin", True, False)])
        with _fake_aliases(default=default, replica=replica), override_settings(DEBUG=True):
            warnings = _check_superuser_connection()
        assert [w.id for w in warnings] == ["django_rls_tenants.W005"]
        assert "'replica_admin' of DATABASES['replica']" in warnings[0].msg
        assert "set it in DATABASES['replica']" in warnings[0].hint

    @pytest.mark.parametrize(
        "error",
        [
            OperationalError("connection refused"),
            InterfaceError("connection already closed"),
            ImproperlyConfigured("settings.DATABASES is improperly configured"),
        ],
        ids=["operational", "interface", "improperly-configured"],
    )
    def test_quiet_skip_when_no_connection_can_be_opened(self, error, caplog):
        """A database that cannot be reached (not provisioned yet) is skipped quietly."""
        conn, cursor = _fake_connection(connect_error=error)
        with (
            _fake_aliases(default=conn),
            caplog.at_level(logging.DEBUG, logger="django_rls_tenants"),
        ):
            assert _check_superuser_connection() == []
        cursor.execute.assert_not_called()
        assert "Skipping W005 and E001 on DATABASES['default']: could not connect" in caplog.text

    def test_w013_when_the_query_fails(self):
        """A failing query on a reachable database is W013, not silence (#108)."""
        error = ProgrammingError(
            "permission denied for view pg_roles\nLINE 1: SELECT rolname FROM pg_roles"
        )
        conn, _ = _fake_connection(query_error=error)
        with _fake_aliases(default=conn), override_settings(DEBUG=False):
            warnings = _check_superuser_connection()
        assert [w.id for w in warnings] == ["django_rls_tenants.W013"]
        assert isinstance(warnings[0], CheckWarning)
        assert (
            "Could not determine whether the connection role bypasses RLS on "
            "DATABASES['default']: the query failed with ProgrammingError: permission "
            "denied for view pg_roles." in warnings[0].msg
        )
        assert "LINE 1" not in warnings[0].msg  # only the first line of the error
        assert "absence of W005 and E001 as a pass" in warnings[0].msg

    def test_w013_when_a_connection_hook_fails(self):
        """A database error other than a failed connect, raised while connecting, is W013."""
        conn, _ = _fake_connection(connect_error=ProgrammingError("clearing the GUCs failed"))
        with _fake_aliases(default=conn):
            warnings = _check_superuser_connection()
        assert [w.id for w in warnings] == ["django_rls_tenants.W013"]
        assert "ProgrammingError: clearing the GUCs failed" in warnings[0].msg

    def test_query_runs_in_a_savepoint_inside_a_transaction(self):
        """In a transaction the query runs in a savepoint, released on success."""
        conn, _ = _fake_connection([("app", False, False)], savepoint="sp1")
        with _fake_aliases(default=conn):
            assert _check_superuser_connection() == []
        conn.savepoint_commit.assert_called_once_with("sp1")
        conn.savepoint_rollback.assert_not_called()

    def test_failed_query_rolls_back_to_its_savepoint(self):
        """A failure rolls back to the savepoint, so the caller's transaction survives."""
        conn, _ = _fake_connection(query_error=ProgrammingError("denied"), savepoint="sp1")
        with _fake_aliases(default=conn):
            warnings = _check_superuser_connection()
        assert [w.id for w in warnings] == ["django_rls_tenants.W013"]
        conn.savepoint_rollback.assert_called_once_with("sp1")
        conn.savepoint_commit.assert_not_called()

    def test_no_savepoint_in_autocommit(self):
        """Outside a transaction Django's ``savepoint()`` is ``None``: nothing to release."""
        conn, _ = _fake_connection(query_error=ProgrammingError("denied"))
        with _fake_aliases(default=conn):
            _check_superuser_connection()
        conn.savepoint_rollback.assert_not_called()
        conn.savepoint_commit.assert_not_called()

    def test_w013_when_the_savepoint_cannot_be_set(self):
        """A transaction already aborted before the check is W013, not a traceback."""
        conn, cursor = _fake_connection()
        conn.savepoint.side_effect = ProgrammingError(
            "current transaction is aborted, commands ignored until end of transaction block"
        )
        with _fake_aliases(default=conn):
            warnings = _check_superuser_connection()
        assert [w.id for w in warnings] == ["django_rls_tenants.W013"]
        assert "current transaction is aborted" in warnings[0].msg
        cursor.execute.assert_not_called()

    def test_w013_on_one_alias_does_not_hide_another(self):
        """Each alias is checked on its own: a failing replica still reports default."""
        default, _ = _fake_connection([("postgres", True, False)])
        replica, _ = _fake_connection(query_error=OperationalError("canceling statement"))
        with _fake_aliases(default=default, replica=replica), override_settings(DEBUG=True):
            warnings = _check_superuser_connection()
        assert [w.id for w in warnings] == [
            "django_rls_tenants.W005",
            "django_rls_tenants.W013",
        ]
        assert "DATABASES['replica']" in warnings[1].msg

    def test_non_postgresql_alias_is_skipped(self):
        conn, _ = _fake_connection(vendor="sqlite")
        with _fake_aliases(default=conn):
            assert _check_superuser_connection() == []
        conn.cursor.assert_not_called()

    def test_an_error_that_is_not_a_database_error_propagates(self):
        """Only database errors are turned into W013; a bug is not swallowed."""
        conn, _ = _fake_connection(query_error=RuntimeError("bug"))
        with _fake_aliases(default=conn), pytest.raises(RuntimeError, match="bug"):
            _check_superuser_connection()

    @pytest.mark.integration
    @pytest.mark.parametrize("debug", [True, False])
    def test_nothing_for_real_plain_role(self, enforce_rls, debug):
        """Against live PostgreSQL, a role RLS applies to gets neither W005 nor E001.

        On the plain-role CI leg this is the connection's own role, so the leg
        asserts that E001 does not fire for the setup the docs recommend.
        """
        with override_settings(DEBUG=debug):
            assert _check_superuser_connection() == []

    @pytest.mark.integration
    @pytest.mark.superuser_only
    @pytest.mark.parametrize(
        ("debug", "check_id"),
        [(True, "django_rls_tenants.W005"), (False, "django_rls_tenants.E001")],
    )
    def test_real_bypassrls_role(self, debug, check_id):
        """Against live PostgreSQL, a non-superuser BYPASSRLS role triggers W005 / E001.

        ``CREATE ROLE`` is transactional, so the role disappears when the
        test transaction rolls back.

        Creating a ``BYPASSRLS`` role needs a superuser connection. On a plain
        role, deselect the test with ``-m "not superuser_only"``; it fails
        instead of skipping so a CI leg whose superuser never materialised
        cannot go green with the check untested (#110).
        """
        if not _is_superuser():
            pytest.fail(
                "test_real_bypassrls_role needs a PostgreSQL superuser connection "
                "to CREATE a BYPASSRLS role, but the test connection is a plain role. "
                'Run as a superuser or deselect it with -m "not superuser_only".'
            )
        role = "rls_w005_bypass_role"
        with connection.cursor() as cur:
            cur.execute(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER BYPASSRLS")
            cur.execute(f"SET ROLE {role}")
        try:
            with override_settings(DEBUG=debug):
                warnings = _check_superuser_connection()
        finally:
            with connection.cursor() as cur:
                cur.execute("RESET ROLE")
        assert [w.id for w in warnings] == [check_id]
        assert f"'{role}' of DATABASES['default'] has the BYPASSRLS attribute" in warnings[0].msg
        assert "superuser and" not in warnings[0].msg

    @pytest.mark.integration
    @pytest.mark.superuser_only
    def test_real_w013_when_pg_roles_is_not_readable(self):
        """Against live PostgreSQL, a role that cannot read ``pg_roles`` gets W013.

        ``REVOKE`` on the catalogue view needs a superuser (see
        ``test_real_bypassrls_role`` for why it fails instead of skipping). The
        check runs its query in a savepoint of its own, so the failure does not
        abort the test transaction: a statement still runs afterwards. The test's
        own savepoint undoes the ``REVOKE``, the ``CREATE ROLE`` and the ``SET ROLE``.
        """
        if not _is_superuser():
            pytest.fail(
                "test_real_w013_when_pg_roles_is_not_readable needs a PostgreSQL superuser "
                "connection to REVOKE SELECT on pg_roles, but the test connection is a plain "
                'role. Run as a superuser or deselect it with -m "not superuser_only".'
            )
        with connection.cursor() as cur:
            cur.execute("SAVEPOINT w013")
            cur.execute("REVOKE SELECT ON pg_catalog.pg_roles FROM PUBLIC")
            cur.execute("CREATE ROLE rls_w013_role NOLOGIN")
            cur.execute("SET ROLE rls_w013_role")
        try:
            warnings = _check_superuser_connection()
            with connection.cursor() as cur:
                cur.execute("SELECT 1")  # the test transaction is not aborted
                assert cur.fetchone() == (1,)
        finally:
            with connection.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT w013")
        assert [w.id for w in warnings] == ["django_rls_tenants.W013"]
        assert "permission denied for view pg_roles" in warnings[0].msg


def _run_policy_default_check_with_rows(rows):
    """Run ``_check_policy_guc_defaults`` against mocked ``pg_db_role_setting`` rows."""
    conn, _ = _fake_connection(rows)
    with _fake_aliases(default=conn):
        return _check_policy_guc_defaults()


class TestCheckPolicyGucDefaults:
    """Tests for _check_policy_guc_defaults (W011, W013).

    A default set with ``ALTER DATABASE ... SET`` or ``ALTER ROLE ... SET`` is in
    force in every new session before any RLS context is set, and the library
    never clears it.
    """

    def test_no_warning_without_defaults(self):
        assert _run_policy_default_check_with_rows([]) == []

    def test_w011_for_a_database_default(self):
        warnings = _run_policy_default_check_with_rows(
            [("app_db", None, ["search_path=public", "rls.is_admin=true"])]
        )

        assert [w.id for w in warnings] == ["django_rls_tenants.W011"]
        assert "rls.is_admin = 'true'" in warnings[0].msg
        assert "for database 'app_db' (DATABASES['default'])" in warnings[0].msg
        assert 'ALTER DATABASE "app_db" RESET rls.is_admin;' in str(warnings[0].hint)

    def test_w011_for_a_role_default(self):
        warnings = _run_policy_default_check_with_rows([(None, "app", ["rls.current_tenant=7"])])

        assert [w.id for w in warnings] == ["django_rls_tenants.W011"]
        assert "for role 'app' in every database" in warnings[0].msg
        assert 'ALTER ROLE "app" RESET rls.current_tenant;' in str(warnings[0].hint)

    def test_w011_for_a_role_default_in_one_database(self):
        """GUC names are compared case-insensitively, as PostgreSQL does."""
        warnings = _run_policy_default_check_with_rows([("app_db", "app", ["RLS.IS_ADMIN=true"])])

        assert [w.id for w in warnings] == ["django_rls_tenants.W011"]
        assert "for role 'app' in database 'app_db'" in warnings[0].msg
        assert 'ALTER ROLE "app" IN DATABASE "app_db" RESET RLS.IS_ADMIN;' in str(warnings[0].hint)

    def test_covers_every_guc_the_policies_read(self):
        """Bypass flags declared on the models' constraints count too, one warning each."""
        names = _policy_guc_names()
        assert len(names) > 2  # the configured pair plus the test models' bypass flags
        warnings = _run_policy_default_check_with_rows(
            [("app_db", None, [f"{name}=true" for name in names])]
        )

        assert [w.id for w in warnings] == ["django_rls_tenants.W011"] * len(names)
        assert [w.msg.split(" = ")[0].rsplit(" ", 1)[1] for w in warnings] == list(names)

    def test_other_gucs_are_ignored(self):
        rows = [("app_db", None, ["work_mem=64MB", "rls.unrelated=1"]), (None, "app", None)]
        assert _run_policy_default_check_with_rows(rows) == []

    def test_every_configured_alias(self):
        """A replica has its own database and role defaults; each alias is read."""
        default, _ = _fake_connection([])
        replica, _ = _fake_connection([("replica_db", None, ["rls.is_admin=on"])])
        with _fake_aliases(default=default, replica=replica):
            warnings = _check_policy_guc_defaults()
        assert [w.id for w in warnings] == ["django_rls_tenants.W011"]
        assert "for database 'replica_db' (DATABASES['replica'])" in warnings[0].msg

    def test_quiet_skip_when_no_connection_can_be_opened(self):
        conn, _ = _fake_connection(connect_error=OperationalError("no database"))
        with _fake_aliases(default=conn):
            assert _check_policy_guc_defaults() == []

    def test_w013_when_the_query_fails(self):
        conn, _ = _fake_connection(
            query_error=ProgrammingError("permission denied for table pg_db_role_setting")
        )
        with _fake_aliases(default=conn):
            warnings = _check_policy_guc_defaults()
        assert [w.id for w in warnings] == ["django_rls_tenants.W013"]
        assert (
            "Could not determine whether PostgreSQL applies a default for a GUC the RLS "
            "policies read on DATABASES['default']" in warnings[0].msg
        )
        assert "absence of W011 as a pass" in warnings[0].msg

    @pytest.mark.integration
    @pytest.mark.superuser_only
    def test_w011_for_real_defaults(self):
        """Against live PostgreSQL: a database default and a role-in-database default.

        Both are set for the test database only, so a parallel run on the same
        server is not affected, and both are transactional and reset again.
        Setting a default for a custom (placeholder) GUC with ``ALTER DATABASE``
        / ``ALTER ROLE`` needs a superuser on PostgreSQL 15+, so on a plain role
        the test fails instead of skipping (deselect it with
        ``-m "not superuser_only"``, see #110).
        """
        if not _is_superuser():
            pytest.fail(
                "test_w011_for_real_defaults needs a PostgreSQL superuser connection to ALTER "
                "DATABASE ... SET a custom GUC, but the test connection is a plain role. "
                'Run as a superuser or deselect it with -m "not superuser_only".'
            )
        with connection.cursor() as cur:
            cur.execute("SELECT current_database()")
            database = connection.ops.quote_name(cur.fetchone()[0])
            cur.execute(f"ALTER DATABASE {database} SET rls.is_admin = 'true'")
            cur.execute(
                f"ALTER ROLE CURRENT_USER IN DATABASE {database} SET rls.current_tenant = '7'"
            )
        try:
            warnings = _check_policy_guc_defaults()
        finally:
            with connection.cursor() as cur:
                cur.execute(f"ALTER DATABASE {database} RESET rls.is_admin")
                cur.execute(
                    f"ALTER ROLE CURRENT_USER IN DATABASE {database} RESET rls.current_tenant"
                )

        assert [w.id for w in warnings] == ["django_rls_tenants.W011"] * 2
        assert "rls.is_admin = 'true'" in warnings[0].msg
        assert "rls.current_tenant = '7'" in warnings[1].msg
        assert "in database" in warnings[1].msg
        assert _check_policy_guc_defaults() == []


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


def _w015(labels):
    """Run the W015 check with ``labels`` as STRICT_MODE_EXCLUDE."""
    with (
        override_settings(
            RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "STRICT_MODE_EXCLUDE": labels},
        ),
        patch(_CONF_PATCH, RLSTenantsConfig()),
    ):
        return _check_strict_mode_exclude()


class TestCheckStrictModeExclude:
    """Tests for _check_strict_mode_exclude (W015)."""

    def test_no_warning_when_unset(self):
        """No W015 with the default (empty) list."""
        assert _w015([]) == []

    def test_no_warning_for_protected_models(self):
        """RLS-protected models, the model name in any case, pass."""
        assert _w015(["test_app.Order", "test_app.protecteduser", "test_app.ORDERITEM"]) == []

    def test_no_warning_for_a_proxy_of_a_protected_model(self):
        """A proxy has no constraints of its own and follows its concrete model."""
        assert _w015(["test_app.OrderProxy"]) == []

    @pytest.mark.parametrize(
        "label",
        ["test_app.Nope", "nope_app.Order", "Order", "test_app.Order.extra"],
        ids=["unknown_model", "unknown_app", "no_dot", "two_dots"],
    )
    def test_w015_unresolvable_label(self, label):
        """A label that does not resolve to an installed model fires W015 naming it."""
        warnings = _w015([label])
        assert [w.id for w in warnings] == ["django_rls_tenants.W015"]
        assert isinstance(warnings[0], CheckWarning)
        assert f"lists {label!r}, which does not resolve to an installed model" in warnings[0].msg

    def test_w015_model_without_rls_constraint(self):
        """A model strict mode never guards (the tenant model) fires W015: the entry is a no-op."""
        warnings = _w015(["test_app.Tenant"])
        assert [w.id for w in warnings] == ["django_rls_tenants.W015"]
        assert "without an RLSConstraint" in warnings[0].msg
        assert "has no effect" in warnings[0].msg

    def test_one_warning_per_bad_label(self):
        """Each bad label gets its own W015; good ones none."""
        warnings = _w015(["test_app.Nope", "test_app.Order", "test_app.Tenant"])
        assert [w.id for w in warnings] == ["django_rls_tenants.W015"] * 2

    def test_runs_without_strict_mode(self):
        """The list is checked whether or not STRICT_MODE is on (it is off in tests)."""
        with override_settings(
            RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "STRICT_MODE": False},
        ):
            assert _w015(["test_app.Nope"])[0].id == "django_rls_tenants.W015"

    @pytest.mark.usefixtures("isolated_apps")
    def test_concrete_model_of_a_protected_proxy_passes(self):
        """A model whose proxy carries the RLSConstraint is guarded through that proxy."""
        plain = type(
            "Plain",
            (models.Model,),
            {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "test_app"})},
        )
        proxy_meta = type(
            "Meta",
            (RLSProtectedModel.Meta,),
            {"app_label": "test_app", "proxy": True},
        )
        proxy = type("PlainProxy", (plain,), {"__module__": __name__, "Meta": proxy_meta})
        with (
            patch.object(apps, "get_models", return_value=[plain, proxy]),
            patch.object(apps, "get_model", return_value=plain),
        ):
            assert _w015(["test_app.Plain"]) == []


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

    def test_w009_reports_a_model_with_a_constraint_but_not_the_base_class(self):
        """W009 covers a model protected by its ``RLSConstraint`` alone (#159)."""
        from tests.test_app.models import Label, OrderProxy  # noqa: PLC0415

        with patch(self._RESOLVER_PATCH, return_value="nonexistent"):
            reported = {w.obj for w in _check_tenant_fk_field_exists()}
        assert Label in reported
        assert OrderProxy not in reported  # a proxy's policy is its concrete model's


@pytest.fixture
def isolated_apps():
    """Build test models in a registry of their own: the check reads only their metadata."""
    with isolate_apps("tests.test_app"):
        yield


def _protected(model_name, /, constraints=(), **attrs):
    """Build an ``RLSProtectedModel`` subclass; ``Meta`` options go in ``meta=``."""
    meta_attrs = {
        "app_label": "test_app",
        "constraints": [*RLSProtectedModel.Meta.constraints, *constraints],
        **attrs.pop("meta", {}),
    }
    meta = type("Meta", (RLSProtectedModel.Meta,), meta_attrs)
    return type(model_name, (RLSProtectedModel,), {"__module__": __name__, "Meta": meta, **attrs})


def _w012(models_, allowlist=None):
    """Run the W012 check over ``models_`` only, with ``allowlist`` as UNIQUE_ACROSS_TENANTS."""
    rls_tenants = {"TENANT_MODEL": "test_app.Tenant"}
    if allowlist is not None:
        rls_tenants["UNIQUE_ACROSS_TENANTS"] = allowlist
    with (
        override_settings(RLS_TENANTS=rls_tenants),
        patch(_CONF_PATCH, RLSTenantsConfig()),
        patch.object(apps, "get_models", return_value=list(models_)),
    ):
        return _check_unique_across_tenants()


@pytest.mark.usefixtures("isolated_apps")
class TestCheckUniqueAcrossTenants:
    """Tests for _check_unique_across_tenants (W012)."""

    def test_no_warning_with_default_config(self):
        """The test app's unique sets include the tenant field, or are allowlisted."""
        assert _check_unique_across_tenants() == []

    def test_unique_field_fires(self):
        """A ``unique=True`` field is reported on its model, with the fix and the allowlist."""
        company = _protected("Company", slug=models.SlugField(unique=True))
        warnings = _w012([company])
        assert [w.id for w in warnings] == ["django_rls_tenants.W012"]
        assert isinstance(warnings[0], CheckWarning)
        assert warnings[0].obj is company
        assert "Field 'slug' (unique=True) is unique across all tenants" in warnings[0].msg
        assert "tenant field 'tenant'" in warnings[0].msg
        assert "IntegrityError" in warnings[0].msg
        assert "UniqueConstraint(fields=['tenant', 'slug'], name=...)" in warnings[0].hint
        assert "{'test_app.Company': ['slug']}" in warnings[0].hint

    def test_one_to_one_field_fires(self):
        """A ``OneToOneField`` is unique too: a profile per user spans every tenant."""
        profile = _protected(
            "Profile",
            user=models.OneToOneField(AdminUser, on_delete=models.CASCADE),
        )
        warnings = _w012([profile])
        assert len(warnings) == 1
        assert "Field 'user' (unique=True)" in warnings[0].msg

    def test_unique_tenant_field_is_scoped(self):
        """A one-row-per-tenant model (a ``OneToOneField`` tenant) is per-tenant by definition."""
        settings_model = _protected(
            "TenantSettings",
            tenant=models.OneToOneField(Tenant, on_delete=models.CASCADE),
        )
        assert _w012([settings_model]) == []

    def test_primary_key_is_not_reported(self):
        """The primary key is unique by nature and not a value one tenant chooses."""
        coded = _protected("Coded", code=models.CharField(max_length=10, primary_key=True))
        assert _w012([coded]) == []

    def test_unique_together_without_tenant_fires(self):
        """A ``unique_together`` entry without the tenant field is reported, as a tuple."""
        company = _protected(
            "Company",
            country=models.CharField(max_length=2),
            vat_id=models.CharField(max_length=20),
            meta={"unique_together": [("country", "vat_id")]},
        )
        warnings = _w012([company])
        assert len(warnings) == 1
        assert "unique_together entry ('country', 'vat_id')" in warnings[0].msg
        assert "fields=['tenant', 'country', 'vat_id']" in warnings[0].hint
        assert "{'test_app.Company': [('country', 'vat_id')]}" in warnings[0].hint

    def test_unique_together_with_tenant_is_scoped(self):
        """A ``unique_together`` entry naming the tenant field, by name or attname, passes."""
        company = _protected(
            "Company",
            slug=models.SlugField(),
            code=models.CharField(max_length=10),
            meta={"unique_together": [("tenant", "slug"), ("code", "tenant_id")]},
        )
        assert _w012([company]) == []

    def test_unique_constraint_without_tenant_fires(self):
        """A field-based ``UniqueConstraint`` without the tenant field is reported."""
        company = _protected(
            "Company",
            constraints=[models.UniqueConstraint(fields=["slug"], name="company_slug_uniq")],
            slug=models.SlugField(),
        )
        warnings = _w012([company])
        assert len(warnings) == 1
        assert "UniqueConstraint 'company_slug_uniq' on ('slug',)" in warnings[0].msg
        assert "{'test_app.Company': ['slug']}" in warnings[0].hint

    def test_conditional_unique_constraint_fires(self):
        """A partial ``UniqueConstraint`` still spans tenants among the rows it covers."""
        company = _protected(
            "Company",
            constraints=[
                models.UniqueConstraint(
                    fields=["slug"],
                    condition=Q(active=True),
                    name="company_active_slug_uniq",
                ),
            ],
            slug=models.SlugField(),
            active=models.BooleanField(default=True),
        )
        assert len(_w012([company])) == 1

    def test_expression_constraint_without_tenant_fires(self):
        """An expression ``UniqueConstraint`` that references no tenant field is reported."""
        company = _protected(
            "Company",
            constraints=[models.UniqueConstraint(Lower("name"), name="company_lower_name")],
            name=models.CharField(max_length=50),
        )
        warnings = _w012([company])
        assert len(warnings) == 1
        assert "UniqueConstraint 'company_lower_name' on expressions" in warnings[0].msg
        assert "reference 'tenant' in the constraint's expressions" in warnings[0].hint
        assert "{'test_app.Company': ['company_lower_name']}" in warnings[0].hint

    @pytest.mark.parametrize(
        "tenant_expression",
        ["tenant", F("tenant"), F("tenant_id"), Lower("tenant")],
        ids=["string", "f", "attname", "nested"],
    )
    def test_expression_constraint_with_tenant_is_scoped(self, tenant_expression):
        """An expression constraint passes when any expression references the tenant field."""
        company = _protected(
            "Company",
            constraints=[
                models.UniqueConstraint(
                    Lower("name"),
                    tenant_expression,
                    name="company_lower_name",
                ),
            ],
            name=models.CharField(max_length=50),
        )
        assert _w012([company]) == []

    def test_every_offending_set_is_reported(self):
        """Each offending unique set gets its own warning, in declaration order."""
        company = _protected(
            "Company",
            constraints=[models.UniqueConstraint(fields=["name"], name="company_name_uniq")],
            slug=models.SlugField(unique=True),
            name=models.CharField(max_length=50),
            code=models.CharField(max_length=10),
            meta={"unique_together": [("code", "name")]},
        )
        warnings = _w012([company])
        assert [w.msg.split(" is unique")[0] for w in warnings] == [
            "Field 'slug' (unique=True)",
            "unique_together entry ('code', 'name')",
            "UniqueConstraint 'company_name_uniq' on ('name',)",
        ]

    def test_models_with_an_rls_constraint_only(self):
        """A model without an ``RLSConstraint`` is not RLS-protected, so it is not checked."""

        class Plain(models.Model):  # noqa: DJ008  -- throwaway model, never printed
            slug = models.SlugField(unique=True)

            class Meta:
                app_label = "test_app"

        assert _w012([Plain]) == []

    def test_proxy_model_is_skipped(self):
        """A proxy shares its concrete model's table, which is reported once."""
        company = _protected("Company", slug=models.SlugField(unique=True))
        proxy = type(
            "CompanyProxy",
            (company,),
            {
                "__module__": __name__,
                "Meta": type("Meta", (), {"proxy": True, "app_label": "test_app"}),
            },
        )
        assert [w.obj for w in _w012([company, proxy])] == [company]

    def test_multi_table_child_reports_only_its_own_fields(self):
        """Any model with an ``RLSConstraint`` is checked; inherited fields stay on the parent."""

        class Parent(models.Model):  # noqa: DJ008  -- throwaway model, never printed
            tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
            code = models.CharField(max_length=10, unique=True)

            class Meta:
                app_label = "test_app"
                constraints = [RLSConstraint(field="tenant", name="parent_rls")]  # noqa: RUF012  -- Django Meta convention

        class Child(Parent):  # noqa: DJ008  -- throwaway model, never printed
            nickname = models.CharField(max_length=10, unique=True)

            class Meta:
                app_label = "test_app"
                constraints = [RLSConstraint(field="tenant", name="child_rls")]  # noqa: RUF012  -- Django Meta convention

        warnings = _w012([Parent, Child])
        assert [(w.obj, w.msg.split(" is unique")[0]) for w in warnings] == [
            (Parent, "Field 'code' (unique=True)"),
            (Child, "Field 'nickname' (unique=True)"),
        ]

    def test_uses_the_constraints_field(self):
        """The tenant field is the ``RLSConstraint``'s ``field``, not ``TENANT_FK_FIELD``."""

        class Account(models.Model):  # noqa: DJ008  -- throwaway model, never printed
            org = models.ForeignKey(Tenant, on_delete=models.CASCADE)
            slug = models.SlugField()

            class Meta:
                app_label = "test_app"
                constraints = [  # noqa: RUF012  -- Django Meta convention
                    RLSConstraint(field="org", name="account_rls"),
                    models.UniqueConstraint(fields=["org", "slug"], name="account_slug_uniq"),
                    models.UniqueConstraint(fields=["slug"], name="account_slug_global"),
                ]

        warnings = _w012([Account])
        assert len(warnings) == 1
        assert "'account_slug_global'" in warnings[0].msg
        assert "tenant field 'org'" in warnings[0].msg

    def test_missing_tenant_field_is_left_to_w009(self):
        """With no tenant field every unique set would fire; W009 reports the model instead."""
        company = _protected("Company", slug=models.SlugField(unique=True))
        with patch("django_rls_tenants.tenants.models._get_tenant_fk_field", return_value="org"):
            assert _w012([company], {"test_app.Company": ["stale"]}) == []

    @pytest.mark.parametrize(
        "entry",
        ["slug", "company_slug_uniq"],
        ids=["field_name", "constraint_name"],
    )
    def test_allowlist_string_silences_single_field_constraint(self, entry):
        """A string names a ``UniqueConstraint`` by name, or a one-field unique set."""
        company = _protected(
            "Company",
            constraints=[models.UniqueConstraint(fields=["slug"], name="company_slug_uniq")],
            slug=models.SlugField(),
        )
        assert _w012([company], {"test_app.Company": [entry]}) == []

    def test_allowlist_silences_a_unique_field(self):
        """A field name silences that ``unique=True`` field; the label is case-insensitive."""
        company = _protected(
            "Company",
            slug=models.SlugField(unique=True),
            user=models.OneToOneField(AdminUser, on_delete=models.CASCADE),
        )
        assert _w012([company], {"TEST_APP.company": ["slug", "user"]}) == []

    @pytest.mark.parametrize(
        "entry",
        [("vat_id", "country"), ["country", "vat_id"], "company_vat_uniq"],
        ids=["tuple_any_order", "list", "constraint_name"],
    )
    def test_allowlist_tuple_silences_multi_field_sets(self, entry):
        """A tuple of field names, in any order, names a multi-field unique set."""
        company = _protected(
            "Company",
            constraints=[
                models.UniqueConstraint(fields=["country", "vat_id"], name="company_vat_uniq"),
            ],
            country=models.CharField(max_length=2),
            vat_id=models.CharField(max_length=20),
        )
        assert _w012([company], {"test_app.Company": [entry]}) == []

    def test_allowlist_tuple_silences_unique_together(self):
        """A tuple names a ``unique_together`` entry, which has no name of its own."""
        company = _protected(
            "Company",
            country=models.CharField(max_length=2),
            vat_id=models.CharField(max_length=20),
            meta={"unique_together": [("country", "vat_id")]},
        )
        assert _w012([company], {"test_app.Company": [("country", "vat_id")]}) == []

    def test_allowlist_field_name_does_not_silence_an_expression(self):
        """Only the name silences an expression constraint: ``Lower(name)`` is not ``name``."""
        company = _protected(
            "Company",
            constraints=[models.UniqueConstraint(Lower("name"), name="company_lower_name")],
            name=models.CharField(max_length=50),
        )
        warnings = _w012([company], {"test_app.Company": ["name"]})
        assert len(warnings) == 2  # the constraint, and the entry that matched nothing
        assert _w012([company], {"test_app.Company": ["company_lower_name"]}) == []

    def test_allowlist_labels_merge_across_case(self):
        """Two spellings of one label list the same model; neither entry is dropped."""
        company = _protected(
            "Company",
            slug=models.SlugField(unique=True),
            code=models.CharField(max_length=10, unique=True),
        )
        allowlist = {"test_app.Company": ["slug"], "TEST_APP.company": ["code"]}
        assert _w012([company], allowlist) == []

    def test_allowlist_is_per_model(self):
        """An entry silences only the model it is listed under."""
        company = _protected("Company", slug=models.SlugField(unique=True))
        brand = _protected("Brand", slug=models.SlugField(unique=True))
        warnings = _w012([company, brand], {"test_app.Company": ["slug"]})
        assert [w.obj for w in warnings] == [brand]

    def test_stale_allowlist_entry_fires(self):
        """An entry that matches no offending unique set is reported, naming it."""
        company = _protected(
            "Company",
            constraints=[
                models.UniqueConstraint(fields=["tenant", "slug"], name="company_slug_uniq"),
            ],
            slug=models.SlugField(),
        )
        warnings = _w012([company], {"test_app.Company": ["slg", "company_slug_uniq"]})
        assert [w.id for w in warnings] == ["django_rls_tenants.W012"] * 2
        assert "lists 'slg', which matches no unique field" in warnings[0].msg
        assert "lists 'company_slug_uniq'" in warnings[1].msg  # already per-tenant
        assert warnings[0].obj is company

    def test_misspelled_allowlist_entry_silences_nothing(self):
        """A typo in an entry leaves the unique field reported and reports the entry too."""
        company = _protected("Company", slug=models.SlugField(unique=True))
        warnings = _w012([company], {"test_app.Company": ["slg"]})
        assert [w.msg.split(" is unique")[0].split(", which")[0] for w in warnings] == [
            "Field 'slug' (unique=True)",
            "RLS_TENANTS['UNIQUE_ACROSS_TENANTS']['test_app.Company'] lists 'slg'",
        ]

    @pytest.mark.parametrize(
        "label",
        ["test_app.Nope", "test_app.Tenant", "nope"],
        ids=["unknown", "not_protected", "malformed"],
    )
    def test_stale_allowlist_label_fires(self, label):
        """A label that names no RLS-protected model is reported."""
        warnings = _w012([], {label: ["slug"]})
        assert [w.id for w in warnings] == ["django_rls_tenants.W012"]
        assert f"lists {label!r}, which names no installed, non-proxy model" in warnings[0].msg

    def test_real_registry_stale_entry(self):
        """Against the real registry, an entry for a per-tenant constraint is stale."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "UNIQUE_ACROSS_TENANTS": {
                        "test_app.Invoice": [("tenant", "number")],
                        **_TEST_APP_ALLOWLIST,
                    },
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_unique_across_tenants()
        assert len(warnings) == 1
        assert "('tenant', 'number')" in warnings[0].msg
        assert warnings[0].obj._meta.label == "test_app.Invoice"


def _model(model_name, meta_bases, /, **meta_attrs):
    """Build an ``RLSProtectedModel`` subclass whose ``Meta`` has ``meta_bases`` and attributes."""
    meta = type("Meta", meta_bases, {"app_label": "test_app", **meta_attrs})
    return type(model_name, (RLSProtectedModel,), {"__module__": __name__, "Meta": meta})


def _w014(models_):
    """Run the W014 check over ``models_`` only."""
    with patch.object(apps, "get_models", return_value=list(models_)):
        return _check_protected_models_have_constraint()


@pytest.mark.usefixtures("isolated_apps")
class TestCheckProtectedModelsHaveConstraint:
    """Tests for _check_protected_models_have_constraint (W014, #46)."""

    def test_no_warning_with_default_config(self):
        """Every RLS-protected model of the test app keeps its RLSConstraint."""
        assert _check_protected_models_have_constraint() == []

    def test_inherited_meta_passes(self):
        assert _w014([_model("Kept", (RLSProtectedModel.Meta,))]) == []

    def test_extended_constraints_pass(self):
        constraints = [
            *RLSProtectedModel.Meta.constraints,
            models.CheckConstraint(condition=Q(id__gt=0), name="kept_positive_id"),
        ]
        model = _model("Extended", (RLSProtectedModel.Meta,), constraints=constraints)
        assert _w014([model]) == []

    def test_meta_without_inheritance(self):
        model = _model("Dropped", ())
        [warning] = _w014([model])
        assert warning.id == "django_rls_tenants.W014"
        assert isinstance(warning, CheckWarning)
        assert warning.obj is model
        assert warning.msg == (
            "test_app.Dropped inherits from RLSProtectedModel but has no RLSConstraint in "
            "Meta.constraints, so no migration creates an RLS policy for its table "
            "'test_app_dropped': the database does not isolate its rows by tenant."
        )
        assert "class Meta(RLSProtectedModel.Meta)" in warning.hint
        assert "[*RLSProtectedModel.Meta.constraints, ...]" in warning.hint

    def test_inherited_meta_with_replaced_constraints(self):
        """``constraints = [...]`` in a Meta that inherits the base replaces its list."""
        model = _model(
            "Replaced",
            (RLSProtectedModel.Meta,),
            constraints=[models.CheckConstraint(condition=Q(id__gt=0), name="positive_id")],
        )
        assert [w.id for w in _w014([model])] == ["django_rls_tenants.W014"]

    def test_proxy_is_skipped(self):
        """A proxy shares its concrete model's table, which W014 reports if it must."""
        concrete = _model("Concrete", (RLSProtectedModel.Meta,))
        proxy_meta = type("Meta", (), {"app_label": "test_app", "proxy": True})
        proxy = type("Proxy", (concrete,), {"__module__": __name__, "Meta": proxy_meta})
        assert _w014([proxy]) == []

    def test_unprotected_model_is_skipped(self):
        """A model that is not an RLSProtectedModel subclass is none of W014's business."""
        plain = type(
            "Plain",
            (models.Model,),
            {"__module__": __name__, "Meta": type("Meta", (), {"app_label": "test_app"})},
        )
        assert _w014([plain]) == []


def _w016(entries, tenant_model="test_app.Tenant"):
    """Run the W016 check with ``entries`` as EXTERNAL_MODELS."""
    with (
        override_settings(
            RLS_TENANTS={"TENANT_MODEL": tenant_model, "EXTERNAL_MODELS": entries},
        ),
        patch(_CONF_PATCH, RLSTenantsConfig()),
    ):
        return _check_external_models()


class TestCheckExternalModels:
    """Tests for _check_external_models (W016, #112)."""

    def test_no_warning_when_unset(self):
        """No W016 with the default (empty) mapping."""
        assert _w016({}) == []

    def test_no_warning_for_the_test_settings(self):
        """``tests/settings.py`` lists Book with its tenant foreign key ``owner``."""
        assert _check_external_models() == []

    def test_no_warning_for_a_valid_entry_in_any_case(self):
        """The model name is case-insensitive, as ``apps.get_model()`` reads it."""
        assert _w016({"test_external_app.book": "owner"}) == []

    @pytest.mark.parametrize(
        "label",
        ["test_external_app.Nope", "nope_app.Book", "Book", "a.b.c"],
        ids=["unknown_model", "unknown_app", "no_dot", "two_dots"],
    )
    def test_unresolvable_label(self, label):
        """A label that names no installed model is reported."""
        warnings = _w016({label: "owner"})
        assert len(warnings) == 1
        assert warnings[0].id == "django_rls_tenants.W016"
        assert f"lists {label!r}, which does not resolve to an installed model" in warnings[0].msg
        assert warnings[0].obj is None

    def test_swapped_out_model_names_its_replacement(self):
        """``auth.User`` is swapped out by ``AUTH_USER_MODEL``: no table, no policy."""
        (warning,) = _w016({"auth.User": "tenant"})
        assert warning.id == "django_rls_tenants.W016"
        assert "names a swapped-out model (replaced by test_app.AdminUser)" in warning.msg
        assert "'test_app.AdminUser'" in warning.hint
        assert warning.obj.__name__ == "User"

    def test_proxy_model_names_its_concrete_model(self):
        """A proxy has no table of its own; the hint names the model to list."""
        (warning,) = _w016({"test_external_app.BookProxy": "owner"})
        assert warning.id == "django_rls_tenants.W016"
        assert "names a proxy model" in warning.msg
        assert "'test_external_app.Book'" in warning.hint
        assert warning.obj.__name__ == "BookProxy"

    def test_model_with_an_rls_constraint(self):
        """Its migrations create the policy, so the entry changes nothing."""
        (warning,) = _w016({"test_app.Order": "tenant"})
        assert "names a model with an RLSConstraint" in warning.msg
        assert "Remove the entry" in warning.hint

    def test_missing_field(self):
        """The field the entry names must exist."""
        (warning,) = _w016({"test_external_app.Book": "tenant"})
        assert "has no field 'tenant'" in warning.msg

    def test_field_that_is_not_a_foreign_key(self):
        """The policy compares a foreign key's column with the tenant."""
        (warning,) = _w016({"test_external_app.Book": "title"})
        assert "maps it to 'title', a CharField, not a ForeignKey" in warning.msg

    def test_foreign_key_to_another_model_than_the_tenant(self):
        """``admin.LogEntry.user`` points at the user model, not at the tenant model."""
        (warning,) = _w016({"admin.LogEntry": "user"})
        assert "a foreign key to test_app.AdminUser" in warning.msg
        assert "RLS_TENANTS['TENANT_MODEL'] (test_app.Tenant)" in warning.msg

    def test_target_is_not_checked_when_the_tenant_model_does_not_resolve(self):
        """An unresolvable TENANT_MODEL is W008's to report."""
        assert _w016({"admin.LogEntry": "user"}, tenant_model="nope.Tenant") == []

    @pytest.mark.parametrize(
        "rls", [{"TENANT_MODEL": "Tenant"}, {}], ids=["no_dot_value_error", "unset_config_error"]
    )
    def test_tenant_model_failures_other_than_lookup_error_are_tolerated(self, rls):
        """A TENANT_MODEL without a dot (ValueError) or unset (RLSConfigurationError) is W008's."""
        with (
            override_settings(RLS_TENANTS={**rls, "EXTERNAL_MODELS": {"admin.LogEntry": "user"}}),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            assert _check_external_models() == []

    def test_hints_name_the_fix(self):
        """Each hint says what to change."""
        (unresolved,) = _w016({"nope_app.Book": "owner"})
        assert "Correct the label to '<app_label>.<ModelName>'" in unresolved.hint
        (missing,) = _w016({"test_external_app.Book": "tenant"})
        assert (
            missing.hint == "Map 'test_external_app.Book' to the name of its tenant foreign key."
        )
        (not_fk,) = _w016({"test_external_app.Book": "title"})
        assert "name of its foreign key to the tenant model" in not_fk.hint
        (other_target,) = _w016({"admin.LogEntry": "user"})
        assert "the policy compares that column with the current tenant" in other_target.hint
        (duplicate,) = _w016(
            {"test_external_app.Book": "owner", "test_external_app.book": "owner"}
        )
        assert duplicate.hint == "Keep one entry per model."

    def test_two_labels_for_one_model(self):
        """Two spellings of one label: only one of their fields would be used."""
        warnings = _w016({"test_external_app.Book": "owner", "test_external_app.book": "owner"})
        assert len(warnings) == 1
        assert "names the same model as 'test_external_app.Book'" in warnings[0].msg

    def test_surfaces_from_real_config(self):
        """A bad entry reaches the aggregated result as W016."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
                    "EXTERNAL_MODELS": {"test_external_app.Book": "missing"},
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.checks._check_superuser_connection",
                return_value=[],
            ),
        ):
            warnings = check_rls_config()
        assert [w.id for w in warnings] == ["django_rls_tenants.W016"]


class TestExternalModelsInOtherChecks:
    """W012 and W015 treat a model listed in EXTERNAL_MODELS as RLS-protected (#112)."""

    def test_w012_reports_a_unique_field_of_an_external_model(self):
        """``AdminUser.username`` is unique across tenants once AdminUser is listed."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "UNIQUE_ACROSS_TENANTS": _TEST_APP_ALLOWLIST,
                    "EXTERNAL_MODELS": {"test_app.AdminUser": "tenant"},
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            warnings = _check_unique_across_tenants()
        assert len(warnings) == 1
        assert warnings[0].obj is AdminUser
        assert "Field 'username' (unique=True)" in warnings[0].msg
        assert "'test_app.AdminUser': ['username']" in warnings[0].hint

    def test_w012_allowlist_entry_for_an_external_model_is_not_stale(self):
        """An allowlist entry for a listed model is matched, not reported as stale."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "UNIQUE_ACROSS_TENANTS": {
                        **_TEST_APP_ALLOWLIST,
                        "test_app.AdminUser": ["username"],
                    },
                    "EXTERNAL_MODELS": {"test_app.AdminUser": "tenant"},
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            assert _check_unique_across_tenants() == []

    def test_w015_accepts_an_external_model_and_its_proxy(self):
        """W015 counts a listed model as RLS-protected, as it counts one with a constraint."""
        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "STRICT_MODE_EXCLUDE": [
                        "test_external_app.Book",
                        "test_external_app.BookProxy",
                    ],
                    "EXTERNAL_MODELS": {"test_external_app.Book": "owner"},
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
        ):
            assert _check_strict_mode_exclude() == []

    def test_w015_reports_an_unlisted_external_model(self):
        """Without its EXTERNAL_MODELS entry, Book is not guarded."""
        (warning,) = _w015(["test_external_app.Book"])
        assert "without an RLSConstraint or EXTERNAL_MODELS entry" in warning.msg


_SHARE = SharedWith(through="test_app.Share", fk="note", tenant_fk="shared_with")


def _sharing_models(*, note_predicates=(_SHARE,), share_field="tenant", share_predicates=()):
    """Build ``Note`` (shared through ``Share``) and ``Share`` in the isolated registry."""

    def meta(constraint):
        return type(
            "Meta",
            (RLSProtectedModel.Meta,),
            {"app_label": "test_app", "constraints": [constraint]},
        )

    note = type(
        "Note",
        (RLSProtectedModel,),
        {
            "__module__": __name__,
            "title": models.CharField(max_length=10),
            "Meta": meta(
                RLSConstraint(field="tenant", name="note_rls", extra_predicates=note_predicates)
            ),
        },
    )
    share = type(
        "Share",
        (RLSProtectedModel,),
        {
            "__module__": __name__,
            "note": models.ForeignKey(note, on_delete=models.CASCADE),
            "shared_with": models.ForeignKey(
                "test_app.Tenant", on_delete=models.CASCADE, related_name="+"
            ),
            "Meta": meta(
                RLSConstraint(
                    field=share_field, name="share_rls", extra_predicates=share_predicates
                )
            ),
        },
    )
    return note, share


def _w017_w018(models_):
    """Run the extra-predicate checks over ``models_`` only."""
    with patch.object(apps, "get_models", return_value=list(models_)):
        return _check_extra_predicates()


class TestCheckExtraPredicatesOfTheTestApp:
    def test_no_warning_with_default_config(self):
        """``SharedNote`` and ``NoteShare`` are set up as W018 asks."""
        assert _check_extra_predicates() == []


@pytest.mark.usefixtures("isolated_apps")
class TestCheckExtraPredicates:
    """Tests for _check_extra_predicates (W017, W018, #59)."""

    def test_share_readable_by_the_tenant_it_shares_with(self):
        models_ = _sharing_models(share_predicates=[TenantField("shared_with")])
        assert _w017_w018(models_) == []

    def test_share_owned_by_the_tenant_it_shares_with(self):
        """The share's own tenant field is ``tenant_fk``: its policy already shows it."""
        assert _w017_w018(_sharing_models(share_field="shared_with")) == []

    def test_w018_share_hidden_from_the_tenant_it_shares_with(self):
        note, share = _sharing_models()
        [warning] = _w017_w018([note, share])
        assert warning.id == "django_rls_tenants.W018"
        assert isinstance(warning, CheckWarning)
        assert warning.obj is note
        assert warning.msg == (
            "SharedWith(through='test_app.Share', fk='note', tenant_fk='shared_with') on "
            "test_app.Note never matches: the RLS policy of test_app.Share filters its rows "
            "on 'tenant', so the tenant in 'shared_with' cannot read the rows shared with "
            "it, inside the sharing policy either."
        )
        assert warning.hint == (
            "Add TenantField('shared_with') to the extra_predicates of test_app.Share's "
            "RLSConstraint and run makemigrations."
        )

    def test_w018_ignores_other_predicates_of_the_share(self):
        models_ = _sharing_models(share_predicates=[RawPredicate("shared_with_id = 1")])
        assert [w.id for w in _w017_w018(models_)] == ["django_rls_tenants.W018"]

    def test_through_model_without_a_policy(self):
        note, _ = _sharing_models()
        plain = type(
            "Plain",
            (models.Model,),
            {
                "__module__": __name__,
                "note": models.ForeignKey(note, on_delete=models.CASCADE),
                "reader": models.ForeignKey(
                    "test_app.Tenant", on_delete=models.CASCADE, related_name="+"
                ),
                "Meta": type("Meta", (), {"app_label": "test_app"}),
            },
        )
        predicate = SharedWith(through="test_app.Plain", fk="note", tenant_fk="reader")
        note._meta.constraints[0].extra_predicates = [predicate]
        assert _w017_w018([note, plain]) == []

    def test_w018_reads_the_concrete_model_of_a_proxy_through_model(self):
        note, share = _sharing_models()
        proxy = type(
            "ShareProxy",
            (share,),
            {
                "__module__": __name__,
                "Meta": type("Meta", (), {"app_label": "test_app", "proxy": True}),
            },
        )
        predicate = SharedWith(through="test_app.ShareProxy", fk="note", tenant_fk="shared_with")
        note._meta.constraints[0].extra_predicates = [predicate]
        [warning] = _w017_w018([note, share, proxy])
        assert warning.id == "django_rls_tenants.W018"
        assert "the RLS policy of test_app.Share filters" in warning.msg

    @pytest.mark.parametrize(
        ("predicate", "problem"),
        [
            (
                SharedWith(through="test_app.Missing", fk="note", tenant_fk="shared_with"),
                "SharedWith on test_app.Note: there is no model 'test_app.Missing'.",
            ),
            (TenantField("title"), "field='title': test_app.Note.title is a CharField"),
        ],
    )
    def test_w017_predicate_that_cannot_be_built(self, predicate, problem):
        note, share = _sharing_models(note_predicates=[predicate])
        [warning] = _w017_w018([note, share])
        assert warning.id == "django_rls_tenants.W017"
        assert warning.obj is note
        assert warning.msg.startswith(
            f"The extra predicate {predicate!r} of test_app.Note's RLSConstraint cannot be "
            f"built: {problem}"
        )
        assert "migrate builds the sharing policy from it" in warning.hint

    def test_w019_shared_with_tenant_fk_to_another_model(self):
        """``tenant_fk`` pointing at the note compares note ids with tenant ids."""
        wrong = SharedWith(through="test_app.Share", fk="note", tenant_fk="note")
        note, share = _sharing_models(
            note_predicates=[wrong], share_predicates=[TenantField("shared_with")]
        )
        [warning] = _w017_w018([note, share])
        assert warning.id == "django_rls_tenants.W019"
        assert warning.obj is note
        assert warning.msg == (
            f"{wrong!r} on test_app.Note compares test_app.Share.note with the current "
            "tenant, but it is a foreign key to test_app.note, not to "
            "RLS_TENANTS['TENANT_MODEL'] (test_app.Tenant): rows would be shared with "
            "whichever tenant has the same key."
        )
        assert warning.hint == (
            "Pass the foreign key to the tenant model as tenant_fk= and run makemigrations."
        )

    def test_w019_tenant_field_to_another_model(self):
        note, share = _sharing_models(share_predicates=[TenantField("note")])
        warnings = _w017_w018([note, share])
        assert [w.id for w in warnings] == [
            "django_rls_tenants.W018",  # the share stays hidden from shared_with
            "django_rls_tenants.W019",
        ]
        assert warnings[1].obj is share
        assert warnings[1].msg.startswith(
            "TenantField('note') on test_app.Share compares test_app.Share.note with the "
            "current tenant, but it is a foreign key to test_app.note"
        )
        assert warnings[1].hint.startswith("Pass the foreign key to the tenant model as field=")

    def test_w019_is_skipped_when_the_tenant_model_does_not_resolve(self, settings):
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "TENANT_MODEL": "nope.Missing"}
        note, share = _sharing_models(share_predicates=[TenantField("note")])
        assert [w.id for w in _w017_w018([note, share])] == ["django_rls_tenants.W018"]

    def test_w017_table_name_that_truncates_to_the_tenant_policys_name(self):
        """PostgreSQL cuts both policy names to 63 bytes: the sharing policy is never created."""
        note, share = _sharing_models(share_predicates=[TenantField("shared_with")])
        table = "n" * 55
        with patch.object(note._meta, "db_table", table):
            [warning] = _w017_w018([note, share])
        assert warning.id == "django_rls_tenants.W017"
        assert warning.obj is note
        assert warning.msg.startswith(
            f"The sharing policy of test_app.Note cannot be created: the table name "
            f"'{table}' is 55 characters long. PostgreSQL cuts an identifier to 63 bytes"
        )
        assert warning.hint.startswith("Set a shorter Meta.db_table")

    def test_proxy_and_models_without_predicates_are_skipped(self):
        note, share = _sharing_models(note_predicates=[TenantField("title")])
        proxy = type(
            "NoteProxy",
            (note,),
            {
                "__module__": __name__,
                "Meta": type("Meta", (), {"app_label": "test_app", "proxy": True}),
            },
        )
        assert [w.id for w in _w017_w018([proxy, share])] == []
