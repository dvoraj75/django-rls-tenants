"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection

from django_rls_tenants.tenants.context import admin_context
from tests.test_app.models import Document, Order, ProtectedUser, Tenant, TenantUser

_RLS_ROLE = "rls_test_role"


@pytest.fixture(scope="session")
def _rls_role(django_db_setup, django_db_blocker):
    """Create a non-superuser role for RLS enforcement (once per session).

    PostgreSQL superusers always bypass RLS, even with FORCE ROW LEVEL
    SECURITY.  This fixture creates a plain role and grants it full DML
    access so that ``SET ROLE`` makes the connection subject to RLS.
    """
    with django_db_blocker.unblock(), connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [_RLS_ROLE])
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {_RLS_ROLE} NOLOGIN")
        cur.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {_RLS_ROLE}")
        cur.execute(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_RLS_ROLE}")


@pytest.fixture
def enforce_rls(_rls_role, db):
    """Switch to a non-superuser role so PostgreSQL RLS is enforced.

    Use this fixture (or the autouse version in test_integration/) for any
    test that verifies database-level row filtering without ``for_user()``.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
        row = cur.fetchone()
        is_super = row is not None and row[0]
    if is_super:
        with connection.cursor() as cur:
            cur.execute(f"SET ROLE {_RLS_ROLE}")
    yield
    if is_super:
        with connection.cursor() as cur:
            cur.execute("RESET ROLE")


@pytest.fixture
def tenant_a(db):
    """Tenant A."""
    return Tenant.objects.create(name="Tenant A")


@pytest.fixture
def tenant_b(db):
    """Tenant B."""
    return Tenant.objects.create(name="Tenant B")


@pytest.fixture
def admin_user(db):
    """Admin user (no tenant, is_admin=True)."""
    return TenantUser.objects.create(
        username="admin",
        tenant=None,
        is_admin=True,
    )


@pytest.fixture
def tenant_a_user(db, tenant_a):
    """Regular user belonging to Tenant A."""
    return TenantUser.objects.create(
        username="user_a",
        tenant=tenant_a,
        is_admin=False,
    )


@pytest.fixture
def tenant_b_user(db, tenant_b):
    """Regular user belonging to Tenant B."""
    return TenantUser.objects.create(
        username="user_b",
        tenant=tenant_b,
        is_admin=False,
    )


@pytest.fixture
def sample_orders(db, tenant_a, tenant_b):
    """Create sample orders for both tenants. Requires admin context for RLS."""
    with admin_context():
        order_a1 = Order.objects.create(
            product="Widget A1",
            amount=Decimal("10.00"),
            tenant=tenant_a,
        )
        order_a2 = Order.objects.create(
            product="Widget A2",
            amount=Decimal("20.00"),
            tenant=tenant_a,
        )
        order_b1 = Order.objects.create(
            product="Gadget B1",
            amount=Decimal("30.00"),
            tenant=tenant_b,
        )
    return {"a1": order_a1, "a2": order_a2, "b1": order_b1}


@pytest.fixture
def sample_documents(db, tenant_a, tenant_b):
    """Create sample documents for both tenants. Requires admin context for RLS."""
    with admin_context():
        doc_a = Document.objects.create(title="Doc A", tenant=tenant_a)
        doc_b = Document.objects.create(title="Doc B", tenant=tenant_b)
    return {"a": doc_a, "b": doc_b}


@pytest.fixture
def sample_protected_users(db, tenant_a, tenant_b):
    """Create sample protected users for both tenants."""
    with admin_context():
        pu_a = ProtectedUser.objects.create(
            email="alice@a.com",
            tenant=tenant_a,
        )
        pu_b = ProtectedUser.objects.create(
            email="bob@b.com",
            tenant=tenant_b,
        )
    return {"a": pu_a, "b": pu_b}
