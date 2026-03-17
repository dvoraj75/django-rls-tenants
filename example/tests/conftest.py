"""Shared fixtures for the example test suite."""

import pytest

from django_rls_tenants.tenants.testing import rls_bypass


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
