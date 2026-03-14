"""End-to-end integration tests.

These tests require a live PostgreSQL database with RLS policies applied.
They verify database-level tenant isolation via both ORM and raw SQL.
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc
from django_rls_tenants.tenants.bypass import bypass_flag
from django_rls_tenants.tenants.context import admin_context, tenant_context
from tests.test_app.models import Document, Order, ProtectedUser

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Tenant isolation -- ORM
# ---------------------------------------------------------------------------


class TestTenantIsolationORM:
    """Verify tenant isolation through the Django ORM."""

    def test_tenant_a_sees_only_own_rows(self, sample_orders, tenant_a):
        """Tenant A context returns only Tenant A's orders."""
        with tenant_context(tenant_a.pk):
            orders = list(Order.objects.values_list("product", flat=True))
        assert sorted(orders) == ["Widget A1", "Widget A2"]

    def test_tenant_b_sees_only_own_rows(self, sample_orders, tenant_b):
        """Tenant B context returns only Tenant B's orders."""
        with tenant_context(tenant_b.pk):
            orders = list(Order.objects.values_list("product", flat=True))
        assert orders == ["Gadget B1"]

    def test_tenant_a_cannot_see_tenant_b(self, sample_orders, tenant_a, tenant_b):
        """Tenant A cannot see Tenant B's rows via ORM."""
        with tenant_context(tenant_a.pk):
            b_orders = Order.objects.filter(tenant_id=tenant_b.pk)
            assert b_orders.count() == 0


class TestAdminAccessORM:
    """Verify admin context returns all rows via ORM."""

    def test_admin_sees_all(self, sample_orders):
        """Admin context returns all tenants' rows."""
        with admin_context():
            assert Order.objects.count() == 3


class TestNoContextORM:
    """Verify fail-closed behavior (no GUC = no data) via ORM."""

    def test_returns_nothing(self, sample_orders):
        """No GUC context = zero rows (fail-closed)."""
        assert Order.objects.count() == 0


# ---------------------------------------------------------------------------
# Tenant isolation -- Raw SQL
# ---------------------------------------------------------------------------


class TestTenantIsolationRawSQL:
    """Verify tenant isolation via raw SQL (bypasses Django ORM)."""

    def test_tenant_sees_own_rows(self, sample_orders, tenant_a):
        """Tenant A context filters rows in raw SQL."""
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute("SELECT product FROM test_order")
            rows = cursor.fetchall()
        products = sorted(r[0] for r in rows)
        assert products == ["Widget A1", "Widget A2"]

    def test_tenant_cannot_see_other(self, sample_orders, tenant_a, tenant_b):
        """Tenant A cannot see Tenant B's rows via raw SQL."""
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM test_order WHERE tenant_id = %s",
                [tenant_b.pk],
            )
            count = cursor.fetchone()[0]
        assert count == 0


class TestAdminAccessRawSQL:
    """Verify admin context returns all rows via raw SQL."""

    def test_admin_sees_all(self, sample_orders):
        """Admin context returns all rows in raw SQL."""
        with admin_context(), connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM test_order")
            count = cursor.fetchone()[0]
        assert count == 3


class TestNoContextRawSQL:
    """Verify fail-closed behavior via raw SQL."""

    def test_returns_nothing(self, sample_orders):
        """No GUC context = zero rows in raw SQL."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM test_order")
            count = cursor.fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Bypass flags
# ---------------------------------------------------------------------------


class TestBypassFlags:
    """Verify extra_bypass_flags on ProtectedUser model.

    The ProtectedUser model has ``extra_bypass_flags=["rls.is_login_request",
    "rls.is_preauth_request"]``. These bypass the USING clause (SELECT) but
    NOT the WITH CHECK clause (INSERT/UPDATE).
    """

    def test_login_bypass_allows_user_lookup(self, sample_protected_users, tenant_a, tenant_b):
        """Login bypass flag allows cross-tenant user lookup on ProtectedUser."""
        # Without tenant context but with login bypass, all users visible
        with bypass_flag("rls.is_login_request"):
            users = list(ProtectedUser.objects.values_list("email", flat=True))
        assert sorted(users) == ["alice@a.com", "bob@b.com"]

    def test_preauth_bypass_allows_user_lookup(self, sample_protected_users):
        """Preauth bypass flag allows cross-tenant user lookup."""
        with bypass_flag("rls.is_preauth_request"):
            assert ProtectedUser.objects.count() == 2

    def test_login_bypass_scoped_to_model(self, sample_orders, sample_protected_users, tenant_a):
        """Login bypass does NOT affect non-user tables (Order).

        The bypass flag is configured only on ProtectedUser's RLSConstraint,
        not on Order's. So rls.is_login_request should have no effect on
        Order queries.
        """
        # Set tenant context for Order isolation + login bypass
        set_guc("rls.is_admin", "false")
        set_guc("rls.current_tenant", str(tenant_a.pk))
        set_guc("rls.is_login_request", "true")
        try:
            # ProtectedUser: login bypass allows seeing all users
            assert ProtectedUser.objects.count() == 2
            # Order: only tenant_a's rows visible (bypass has no effect)
            orders = list(Order.objects.values_list("product", flat=True))
            assert sorted(orders) == ["Widget A1", "Widget A2"]
        finally:
            clear_guc("rls.is_admin")
            clear_guc("rls.current_tenant")
            clear_guc("rls.is_login_request")

    def test_bypass_flag_no_leak(self, sample_protected_users, tenant_a):
        """Bypass flag does not persist after context manager exits."""
        with bypass_flag("rls.is_login_request"):
            assert ProtectedUser.objects.count() == 2
        # After exit, bypass is cleared
        assert get_guc("rls.is_login_request") is None
        # Without bypass and without tenant context, fail-closed
        assert ProtectedUser.objects.count() == 0

    def test_preauth_bypass_does_not_affect_order(self, sample_orders, sample_protected_users):
        """Preauth bypass on ProtectedUser doesn't leak to Order table.

        Derived from reference/tests_rls_bypass_flags.py patterns.
        """
        with bypass_flag("rls.is_preauth_request"):
            # ProtectedUser: preauth bypass allows seeing all
            assert ProtectedUser.objects.count() == 2
            # Order: no bypass configured, no context set -> zero rows
            assert Order.objects.count() == 0


# ---------------------------------------------------------------------------
# Write operations (INSERT / UPDATE)
# ---------------------------------------------------------------------------


class TestWriteOperations:
    """Verify RLS WITH CHECK enforcement on writes."""

    def test_insert_with_admin_context(self, tenant_a):
        """INSERT with admin context succeeds."""
        with admin_context():
            order = Order.objects.create(
                product="New Widget",
                amount=Decimal("5.00"),
                tenant=tenant_a,
            )
        assert order.pk is not None

    def test_insert_with_matching_tenant_context(self, tenant_a):
        """INSERT with matching tenant context succeeds."""
        with tenant_context(tenant_a.pk):
            order = Order.objects.create(
                product="Tenant Widget",
                amount=Decimal("7.50"),
                tenant=tenant_a,
            )
        assert order.pk is not None

    @pytest.mark.django_db(transaction=True)
    def test_insert_without_context_fails(self, tenant_a):
        """INSERT without RLS context violates WITH CHECK (fail-closed).

        PostgreSQL raises an RLS violation when the WITH CHECK clause
        is not satisfied. Django wraps this as an InternalError or
        DatabaseError.
        """
        from django.db.utils import InternalError  # noqa: PLC0415  -- needed only in this test

        with pytest.raises((InternalError, Exception)):
            Order.objects.create(
                product="Ghost Widget",
                amount=Decimal("1.00"),
                tenant=tenant_a,
            )

    @pytest.mark.django_db(transaction=True)
    def test_insert_with_wrong_tenant_context_fails(self, tenant_a, tenant_b):
        """INSERT with a different tenant's context violates WITH CHECK.

        The WITH CHECK clause requires tenant_id to match the GUC variable.
        Inserting tenant_a's data while context is set to tenant_b should fail.
        """
        from django.db.utils import InternalError  # noqa: PLC0415  -- needed only in this test

        with pytest.raises((InternalError, Exception)), tenant_context(tenant_b.pk):
            Order.objects.create(
                product="Wrong Tenant Widget",
                amount=Decimal("2.00"),
                tenant=tenant_a,
            )


# ---------------------------------------------------------------------------
# Cross-model isolation
# ---------------------------------------------------------------------------


class TestCrossModelIsolation:
    """Verify isolation across multiple RLS-protected models."""

    def test_tenant_context_isolates_both_models(self, sample_orders, sample_documents, tenant_a):
        """Tenant context isolates both Order and Document."""
        with tenant_context(tenant_a.pk):
            assert Order.objects.count() == 2
            assert Document.objects.count() == 1

    def test_no_context_blocks_both_models(self, sample_orders, sample_documents):
        """No context blocks access to all RLS-protected models."""
        assert Order.objects.count() == 0
        assert Document.objects.count() == 0


# ---------------------------------------------------------------------------
# Management command: check_rls
# ---------------------------------------------------------------------------


class TestCheckRlsCommand:
    """Verify the ``check_rls`` management command."""

    def test_succeeds_when_rls_applied(self):
        """Command exits cleanly when all RLS-protected tables are correct."""
        out = StringIO()
        call_command("check_rls", stdout=out)
        output = out.getvalue()
        assert "verified" in output.lower()

    def test_reports_models(self):
        """Command output lists each RLS-protected model and its policies."""
        out = StringIO()
        call_command("check_rls", stdout=out)
        output = out.getvalue()
        # At least Order, Document, ProtectedUser should be listed
        assert "Order" in output
        assert "Document" in output
        assert "ProtectedUser" in output
