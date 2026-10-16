"""Tests for django_rls_tenants.pytest_plugin."""

from __future__ import annotations

import subprocess
import sys

import pytest
from asgiref.sync import async_to_sync
from django.db import connection

from django_rls_tenants import pytest_plugin
from django_rls_tenants.pytest_plugin import _bypasses_rls, _prepare_role, _switch_role
from django_rls_tenants.tenants.testing import aassert_rls_enforced, assert_rls_enforced


def _is_superuser():
    with connection.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = session_user")
        return cur.fetchone()[0]


def _require_superuser(test_name, reason):
    """Fail (never skip) a ``superuser_only`` test on a plain role (#110)."""
    if not _is_superuser():
        pytest.fail(
            f"{test_name} needs a PostgreSQL superuser connection to {reason}, but the test "
            'connection is a plain role. Run as a superuser or deselect it with -m "not '
            'superuser_only".'
        )


def _current_user():
    with connection.cursor() as cur:
        cur.execute("SELECT current_user")
        return cur.fetchone()[0]


class _Config:
    """Stand-in for ``pytest.Config`` with the plugin's two ini options."""

    def __init__(self, *, role="rls_test_role", aliases=("default",)):
        self._ini = {"rls_role": role, "rls_role_aliases": list(aliases)}

    def getini(self, name):
        return self._ini[name]


class TestRegistration:
    """The ``pytest11`` entry point and the ini options."""

    def test_loaded_through_the_entry_point(self, pytestconfig):
        """pytest registers the module under the entry point name ``django_rls_tenants``."""
        assert pytestconfig.pluginmanager.get_plugin("django_rls_tenants") is pytest_plugin

    def test_import_does_not_import_django(self):
        """pytest imports the plugin at startup, before Django settings exist."""
        code = (
            "import sys\n"
            "import django_rls_tenants.pytest_plugin\n"
            "loaded = sorted(m for m in sys.modules if m == 'django' or m.startswith('django.'))\n"
            "assert not loaded, loaded\n"
        )
        result = subprocess.run(  # noqa: S603  -- fixed interpreter and code
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_ini_defaults(self, pytestconfig):
        """The suite sets neither option, so it runs on the defaults."""
        assert pytestconfig.getini("rls_role") == "rls_test_role"
        assert pytestconfig.getini("rls_role_aliases") == ["default"]

    @pytest.mark.parametrize(
        "role",
        ["", "RLS_Role", "1role", "role-name", 'x"; DROP ROLE postgres; --', "r" * 64],
    )
    def test_configure_rejects_an_invalid_role_name(self, role):
        """Only a lowercase unquoted identifier can go into SET ROLE and ``-c role=``."""
        with pytest.raises(pytest.UsageError, match="is not a valid role name"):
            pytest_plugin.pytest_configure(_Config(role=role))

    def test_configure_rejects_no_aliases(self):
        """An empty alias list would make ``enforce_rls`` a silent no-op."""
        with pytest.raises(pytest.UsageError, match="rls_role_aliases is empty"):
            pytest_plugin.pytest_configure(_Config(aliases=()))

    def test_configure_accepts_valid_options(self):
        """A valid role name and alias list pass."""
        pytest_plugin.pytest_configure(_Config(role="my_rls_role", aliases=("default", "other")))

    def test_aliases_drop_duplicates_in_order(self):
        """Listing an alias twice switches it once."""
        config = _Config(aliases=("default", "other", "default"))
        assert pytest_plugin._aliases(config) == ["default", "other"]


@pytest.mark.django_db
class TestEnforceRls:
    """The ``enforce_rls`` fixture."""

    def test_rls_applies_under_enforce_rls(self, enforce_rls):
        """On a superuser leg the fixture switched roles; on a plain role it did nothing."""
        assert_rls_enforced()
        assert not _bypasses_rls(connection)

    def test_async_assertion_under_enforce_rls(self, enforce_rls):
        """The async twin checks the same connection when driven by ``async_to_sync``."""
        async_to_sync(aassert_rls_enforced)()

    @pytest.mark.superuser_only
    def test_switches_to_the_configured_role(self, enforce_rls, rls_role):
        """On a superuser connection ``current_user`` becomes ``rls_role``."""
        _require_superuser("test_switches_to_the_configured_role", "switch roles")
        assert _current_user() == rls_role

    @pytest.mark.django_db(databases=[])
    def test_leaves_an_alias_the_test_may_not_use_alone(self, enforce_rls):
        """With no database allowed, switching ``default`` would raise; the fixture skips it.

        Django forbids any query on ``default`` in this test, so the fixture
        reaching the test body is the assertion.
        """

    @pytest.mark.django_db(transaction=True)
    def test_a_reopened_connection_is_switched_again(self, enforce_rls):
        """A connection the test closes comes back as ``rls_role``, not as the login role."""
        connection.close()
        assert_rls_enforced()
        assert not _bypasses_rls(connection)

    @pytest.mark.superuser_only
    @pytest.mark.django_db(transaction=True)
    def test_a_reopened_connection_runs_as_the_configured_role(self, enforce_rls, rls_role):
        """The receiver switched the new session (and the teardown resets it)."""
        _require_superuser("test_a_reopened_connection_runs_as_the_configured_role", "switch")
        connection.close()
        assert _current_user() == rls_role

    def test_rls_role_fails_inside_a_test_transaction(self):
        """First requested inside the test transaction, the role would roll back: fail."""
        assert connection.in_atomic_block
        with pytest.raises(pytest.fail.Exception, match="inside a transaction") as excinfo:
            pytest_plugin._fail_inside_a_test_transaction(connection)
        assert "request.getfixturevalue()" in str(excinfo.value)

    @pytest.mark.django_db(transaction=True)
    def test_rls_role_passes_outside_a_test_transaction(self):
        """Outside a transaction (``transaction=True``) the role is created for good."""
        assert not connection.in_atomic_block
        pytest_plugin._fail_inside_a_test_transaction(connection)

    def test_tables_created_later_are_granted(self, request, rls_role):
        """``ALTER DEFAULT PRIVILEGES`` covers a table the connection's role creates later.

        Migrations run in a test (django-test-migrations, a dynamic model) create
        tables after the session's ``GRANT ... ON ALL TABLES``.
        """
        with connection.cursor() as cur:
            cur.execute("CREATE TABLE rls_plugin_later (id serial PRIMARY KEY, label text)")
        request.getfixturevalue("enforce_rls")
        with connection.cursor() as cur:
            cur.execute("INSERT INTO rls_plugin_later (label) VALUES ('x')")
            cur.execute("SELECT label FROM rls_plugin_later")
            assert cur.fetchall() == [("x",)]


@pytest.mark.django_db
class TestAssertRlsEnforced:
    """``assert_rls_enforced()`` on a connection that bypasses RLS."""

    @pytest.mark.superuser_only
    def test_fails_on_a_superuser(self):
        """The bare superuser connection bypasses every policy."""
        _require_superuser("test_fails_on_a_superuser", "be one")
        with pytest.raises(AssertionError, match="is a PostgreSQL superuser") as excinfo:
            assert_rls_enforced()
        assert "enforce_rls" in str(excinfo.value)

    @pytest.mark.superuser_only
    def test_fails_on_a_bypassrls_role(self):
        """A non-superuser ``BYPASSRLS`` role bypasses them too (rolled back with the test)."""
        _require_superuser("test_fails_on_a_bypassrls_role", "CREATE a BYPASSRLS role")
        with connection.cursor() as cur:
            cur.execute("CREATE ROLE rls_plugin_bypass NOLOGIN BYPASSRLS")
            cur.execute("SET ROLE rls_plugin_bypass")
        try:
            with pytest.raises(AssertionError, match="has the BYPASSRLS attribute") as excinfo:
                assert_rls_enforced()
        finally:
            with connection.cursor() as cur:
                cur.execute("RESET ROLE")
        assert "is a PostgreSQL superuser" not in str(excinfo.value)

    @pytest.mark.superuser_only
    def test_async_twin_fails_on_a_superuser(self):
        """``aassert_rls_enforced()`` raises the same assertion."""
        _require_superuser("test_async_twin_fails_on_a_superuser", "be one")
        with pytest.raises(AssertionError, match="bypasses every RLS policy"):
            async_to_sync(aassert_rls_enforced)()


@pytest.mark.django_db
class TestPrepareRole:
    """Creating and granting the role (``CREATE ROLE`` and ``GRANT`` roll back with the test)."""

    @pytest.mark.superuser_only
    def test_creates_a_missing_role_that_rls_applies_to(self):
        """A missing role is created ``NOLOGIN`` and can read the tables after ``SET ROLE``."""
        _require_superuser("test_creates_a_missing_role_that_rls_applies_to", "CREATE ROLE")
        _prepare_role(connection, "rls_plugin_new")
        with connection.cursor() as cur:
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = 'rls_plugin_new'"
            )
            assert cur.fetchone() == (False, False, False)
        assert _switch_role(connection, "rls_plugin_new") is True
        try:
            assert_rls_enforced()
            with connection.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM django_migrations")
                assert cur.fetchone()[0] > 0
        finally:
            with connection.cursor() as cur:
                cur.execute("RESET ROLE")

    @pytest.mark.superuser_only
    def test_rejects_a_role_that_bypasses_rls(self):
        """``SET ROLE`` to a ``BYPASSRLS`` role would leave RLS bypassed: fail loudly."""
        _require_superuser("test_rejects_a_role_that_bypasses_rls", "CREATE a BYPASSRLS role")
        with connection.cursor() as cur:
            cur.execute("CREATE ROLE rls_plugin_bad NOLOGIN BYPASSRLS")
        with pytest.raises(pytest.fail.Exception, match="is a superuser or has BYPASSRLS"):
            _prepare_role(connection, "rls_plugin_bad")

    def test_noop_on_a_plain_role(self, enforce_rls):
        """A connection RLS already applies to creates nothing (it usually lacks CREATEROLE)."""
        _prepare_role(connection, "rls_plugin_never")
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rls_plugin_never'")
            assert cur.fetchone() is None

    def test_switch_role_noop_on_a_plain_role(self, enforce_rls):
        """``_switch_role`` reports that it did nothing, so nothing is reset."""
        user = _current_user()
        assert _switch_role(connection, "rls_plugin_never") is False
        assert _current_user() == user
