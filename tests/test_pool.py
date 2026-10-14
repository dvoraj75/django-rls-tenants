"""Tests for django_rls_tenants.pool.

Driver-independent: the end-to-end tests against a real ``psycopg_pool`` pool are in
``tests/test_integration/test_connection_pool.py`` (psycopg 3 only).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from django.apps import apps
from django.conf import settings as django_settings
from django.core.signals import setting_changed
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.pool import (
    _build_clear_gucs_sql,
    _clear_policy_gucs,
    _declared_policy_guc_names_cached,
    _policy_guc_names,
    _pool_options,
    reset_rls_gucs,
)
from django_rls_tenants.rls.constraints import RLSConstraint, RLSM2MConstraint
from django_rls_tenants.rls.guc import get_guc, set_guc
from tests.test_app.models import Order, Project

# Configured tenant and admin GUCs, then ProtectedUser's extra_bypass_flags.
_POLICY_GUCS = (
    "rls.is_admin",
    "rls.current_tenant",
    "rls.is_login_request",
    "rls.is_preauth_request",
)


class _RecordingConnection:
    """Stands in for a psycopg 3 connection: records statements and commits."""

    def __init__(self, *, autocommit):
        self.autocommit = autocommit
        self.executed = []
        self.commits = 0

    def execute(self, query, params):
        self.executed.append((query, tuple(params)))

    def commit(self):
        self.commits += 1


class _BrokenConnection(_RecordingConnection):
    def execute(self, query, params):
        raise RuntimeError("connection lost")


@pytest.fixture
def fresh_registry_scan():
    """Make ``_policy_guc_names`` scan the model registry again, before and after the test."""
    _declared_policy_guc_names_cached.cache_clear()
    yield
    _declared_policy_guc_names_cached.cache_clear()


class TestPolicyGucNames:
    """``_policy_guc_names`` lists every GUC the library's policies read."""

    def test_configured_names_then_declared_names(self):
        names = _policy_guc_names()

        assert names[:2] == ("rls.is_admin", "rls.current_tenant")
        assert sorted(names) == sorted(_POLICY_GUCS)

    def test_follows_guc_prefix_and_keeps_names_the_policies_declare(self):
        """The installed policies still read ``rls.*`` (W001 / W002): those stay too."""
        rls_tenants = {**django_settings.RLS_TENANTS, "GUC_PREFIX": "myapp"}
        with override_settings(RLS_TENANTS=rls_tenants):
            names = _policy_guc_names()

        assert names[:2] == ("myapp.is_admin", "myapp.current_tenant")
        assert sorted(names[2:]) == sorted(_POLICY_GUCS)

    def test_includes_m2m_constraint_names(self, fresh_registry_scan):
        through = Project.members.through
        custom = RLSM2MConstraint(
            name="custom_m2m",
            from_model="test_app.Project",
            to_model="test_app.ProtectedUser",
            from_fk="project_id",
            to_fk="protecteduser_id",
            guc_tenant_var="custom.tenant",
            guc_admin_var="custom.admin",
        )
        with patch.object(through._meta, "constraints", [*through._meta.constraints, custom]):
            names = _policy_guc_names()

        assert {"custom.tenant", "custom.admin"} <= set(names)

    @pytest.mark.parametrize(
        "flag",
        ["is_login_request", "rls.", "rls..login", "search_path"],
    )
    def test_leaves_out_names_postgresql_rejects_or_owns(self, fresh_registry_scan, flag):
        """Such a name cannot hold a session value, and would fail the whole clear."""
        odd = RLSConstraint(field="tenant", name="odd_constraint", extra_bypass_flags=[flag])
        with patch.object(Order._meta, "constraints", [*Order._meta.constraints, odd]):
            names = _policy_guc_names()

        assert flag not in names
        assert sorted(names) == sorted(_POLICY_GUCS)

    def test_reads_the_registry_afresh_until_it_is_ready(self, fresh_registry_scan):
        """A constraint added during ``ready()`` (``register_m2m_rls``) is not missed."""
        late = RLSConstraint(
            field="tenant",
            name="late_constraint",
            extra_bypass_flags=["rls.is_late_request"],
        )
        with (
            patch.object(Order._meta, "constraints", [*Order._meta.constraints, late]),
            patch.object(apps, "ready", False),
        ):
            assert "rls.is_late_request" in _policy_guc_names()
            assert _declared_policy_guc_names_cached.cache_info().currsize == 0

    def test_caches_the_registry_once_it_is_ready(self, fresh_registry_scan):
        _policy_guc_names()
        _policy_guc_names()

        assert _declared_policy_guc_names_cached.cache_info().hits == 1

    def test_installed_apps_change_drops_the_cache(self, fresh_registry_scan):
        _policy_guc_names()
        assert _declared_policy_guc_names_cached.cache_info().currsize == 1

        setting_changed.send(
            sender=type(django_settings),
            setting="INSTALLED_APPS",
            value=django_settings.INSTALLED_APPS,
            enter=True,
        )

        assert _declared_policy_guc_names_cached.cache_info().currsize == 0


class TestBuildClearGucsSql:
    """``_build_clear_gucs_sql`` names every GUC by a bind parameter."""

    def test_one_set_config_per_name(self):
        assert _build_clear_gucs_sql(2) == (
            "SELECT set_config(%s, '', false), set_config(%s, '', false)"
        )


@pytest.mark.django_db
class TestClearPolicyGucs:
    """``_clear_policy_gucs`` clears every policy GUC in one statement."""

    def test_clears_every_policy_guc(self):
        for name in _POLICY_GUCS:
            set_guc(name, "42" if name == "rls.current_tenant" else "true")

        _clear_policy_gucs(connection)

        assert [get_guc(name) for name in _POLICY_GUCS] == [None] * len(_POLICY_GUCS)

    def test_one_round_trip(self):
        # Settle the connection first: after a transaction an earlier test ended
        # with a driver commit, the GUC affinity guard would apply the (empty)
        # context once before the first statement.
        get_guc("rls.current_tenant")
        with CaptureQueriesContext(connection) as queries:
            _clear_policy_gucs(connection)

        assert len(queries) == 1

    def test_no_statement_without_names(self):
        """An invalid ``GUC_PREFIX`` and no valid declared name leave nothing to clear."""
        with (
            patch("django_rls_tenants.pool._policy_guc_names", return_value=()),
            CaptureQueriesContext(connection) as queries,
        ):
            _clear_policy_gucs(connection)

        assert len(queries) == 0

    @pytest.mark.django_db(transaction=True)
    def test_committed_outside_autocommit(self):
        """With ``AUTOCOMMIT=False`` a later rollback must not restore the old values."""
        set_guc("rls.current_tenant", "777")
        connection.set_autocommit(False)
        try:
            _clear_policy_gucs(connection)
            connection.rollback()

            assert get_guc("rls.current_tenant") is None
        finally:
            connection.rollback()
            connection.set_autocommit(True)


class TestResetRlsGucs:
    """``reset_rls_gucs`` on a stand-in connection (the real pool is tested end to end)."""

    def test_autocommit_connection_one_statement_no_commit(self):
        conn = _RecordingConnection(autocommit=True)

        reset_rls_gucs(conn)

        assert conn.executed == [(_build_clear_gucs_sql(len(_POLICY_GUCS)), _policy_guc_names())]
        assert sorted(conn.executed[0][1]) == sorted(_POLICY_GUCS)
        assert conn.commits == 0

    def test_commits_outside_autocommit_so_the_connection_is_left_idle(self):
        conn = _RecordingConnection(autocommit=False)

        reset_rls_gucs(conn)

        assert len(conn.executed) == 1
        assert conn.commits == 1

    def test_no_statement_without_names(self):
        conn = _RecordingConnection(autocommit=False)

        with patch("django_rls_tenants.pool._policy_guc_names", return_value=()):
            reset_rls_gucs(conn)

        assert (conn.executed, conn.commits) == ([], 0)

    def test_failure_propagates_so_the_pool_discards_the_connection(self):
        with pytest.raises(RuntimeError, match="connection lost"):
            reset_rls_gucs(_BrokenConnection(autocommit=True))


class TestPoolOptions:
    """``_pool_options`` mirrors Django's test for an enabled pool."""

    @pytest.mark.parametrize(
        ("settings_dict", "expected"),
        [
            ({}, None),
            ({"OPTIONS": {}}, None),
            ({"OPTIONS": {"pool": False}}, None),
            ({"OPTIONS": {"pool": None}}, None),
            # Django treats an empty dict as "no pool" too.
            ({"OPTIONS": {"pool": {}}}, None),
            ({"OPTIONS": {"pool": True}}, {}),
            ({"OPTIONS": {"pool": {"max_size": 4}}}, {"max_size": 4}),
        ],
    )
    def test_pool_options(self, settings_dict, expected):
        assert _pool_options(settings_dict) == expected


def test_importable_from_settings_before_django_setup():
    """``settings.py`` imports ``reset_rls_gucs`` before Django is set up.

    The ``tenants`` package imports models in its ``__init__``, which would
    fail at that point, so ``pool`` must not import it at module level.
    """
    code = (
        "import sys\n"
        "from django.conf import settings\n"
        "from django_rls_tenants.pool import reset_rls_gucs\n"
        "assert not settings.configured\n"
        "loaded = [m for m in sys.modules if m.startswith('django_rls_tenants.tenants')]\n"
        "assert not loaded, loaded\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    result = subprocess.run(  # noqa: S603  -- fixed interpreter and code
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
