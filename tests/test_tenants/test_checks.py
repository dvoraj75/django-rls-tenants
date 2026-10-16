"""Tests for django_rls_tenants.tenants.checks."""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
from django.core.checks import Error as CheckError
from django.core.checks import Warning as CheckWarning
from django.core.exceptions import ImproperlyConfigured
from django.db import InterfaceError, OperationalError, ProgrammingError, connection
from django.test import override_settings

from django_rls_tenants.pool import _policy_guc_names, reset_rls_gucs
from django_rls_tenants.tenants.checks import (
    _check_conn_max_age_with_session_gucs,
    _check_databases_alias_exists,
    _check_guc_prefix_mismatch,
    _check_policy_guc_defaults,
    _check_pool_without_reset,
    _check_superuser_connection,
    _check_tenant_fk_field_exists,
    _check_tenant_model_resolves,
    _checked_aliases,
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
            override_settings(RLS_TENANTS={"TENANT_MODEL": "nope.Nope"}),
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

    def test_w010_surfaces_from_real_config(self):
        """A pooled alias without a reset reaches the aggregated result as W010."""
        with (
            override_settings(
                RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant"},
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
                RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "DATABASES": ["default", "nope"]}
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


def _fake_connection(rows=(), *, connect_error=None, query_error=None, vendor="postgresql"):
    """Return a mocked connection whose catalogue query returns ``rows``, and its cursor.

    ``connect_error`` is raised by ``cursor()`` (opening the connection),
    ``query_error`` by ``execute()`` (the query itself).
    """
    cursor = MagicMock()
    cursor.fetchall.return_value = list(rows)
    cursor.__enter__ = lambda self: self
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.execute.side_effect = query_error
    conn = MagicMock()
    conn.vendor = vendor
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
        failed query aborts the transaction, so the test works in a savepoint and
        rolls back to it: that also undoes the ``REVOKE``, the ``CREATE ROLE`` and the
        ``SET ROLE``.
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
