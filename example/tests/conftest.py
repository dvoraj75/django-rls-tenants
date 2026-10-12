"""Shared fixtures for the example test suite."""

import os

import pytest

from django_rls_tenants.tenants.testing import rls_bypass

# Zero-skipped-tests rule, mirrored from the library's tests/conftest.py (#110).
# CI runs this suite against the built wheel as a non-superuser role with
# RLS_FAIL_ON_SKIPPED=1; a skipped fail-closed test must not pass the job.
_FAIL_ON_SKIPPED_ENV = "RLS_FAIL_ON_SKIPPED"


def pytest_sessionfinish(session, exitstatus):
    """Fail the session on any skipped test when ``RLS_FAIL_ON_SKIPPED=1``."""
    if os.environ.get(_FAIL_ON_SKIPPED_ENV) != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    skipped = reporter.stats.get("skipped", [])
    if not skipped:
        return
    reporter.write_sep(
        "=",
        f"{len(skipped)} skipped test(s) but {_FAIL_ON_SKIPPED_ENV}=1: the session fails",
        red=True,
        bold=True,
    )
    for report in skipped:
        longrepr = getattr(report, "longrepr", None)
        reason = longrepr[-1] if isinstance(longrepr, tuple) else ""
        reporter.write_line(f"  {report.nodeid}: {reason}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def tenant_acme(db):
    """Create a test tenant."""
    from tenants.models import Tenant

    with rls_bypass():
        return Tenant.objects.create(name="Acme Corp", slug="acme-test")


@pytest.fixture
def tenant_globex(db):
    """Create a second test tenant."""
    from tenants.models import Tenant

    with rls_bypass():
        return Tenant.objects.create(name="Globex Inc", slug="globex-test")


@pytest.fixture
def acme_user(db, tenant_acme):
    """Create a regular tenant user for Acme."""
    from accounts.models import User

    with rls_bypass():
        return User.objects.create_user(
            username="testuser",
            email="test@acme.com",
            password="testpass",  # noqa: S106 -- demo fixture
            tenant=tenant_acme,
            is_tenant_admin=False,
        )


@pytest.fixture
def admin_user(db):
    """Create an admin user (no tenant, sees everything)."""
    from accounts.models import User

    with rls_bypass():
        return User.objects.create_user(
            username="testadmin",
            email="admin@test.com",
            password="testpass",  # noqa: S106 -- demo fixture
            tenant=None,
            is_tenant_admin=True,
        )
