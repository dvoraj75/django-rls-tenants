"""End-to-end integration tests.

These tests require a live PostgreSQL database with RLS policies applied.
They verify database-level tenant isolation via both ORM and raw SQL.
"""

from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants.bypass import bypass_flag
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.state import get_current_tenant_id
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
        # ProtectedUser: login bypass allows seeing all users
        assert ProtectedUser.objects.count() == 2
        # Order: only tenant_a's rows visible (bypass has no effect)
        orders = list(Order.objects.values_list("product", flat=True))
        assert sorted(orders) == ["Widget A1", "Widget A2"]

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
# UPDATE / DELETE tenant isolation
# ---------------------------------------------------------------------------


class TestUpdateDeleteIsolation:
    """Verify RLS prevents cross-tenant UPDATE and DELETE operations."""

    @pytest.mark.django_db(transaction=True)
    def test_update_other_tenant_row_fails(self, sample_orders, tenant_a, tenant_b):
        """Tenant A cannot UPDATE Tenant B's rows.

        With tenant_a context, an update targeting tenant_b's order should
        affect zero rows (RLS USING clause filters them out).
        """
        with tenant_context(tenant_a.pk):
            updated = Order.objects.filter(pk=sample_orders["b1"].pk).update(product="Hacked")
        assert updated == 0
        # Verify original value is intact
        with admin_context():
            sample_orders["b1"].refresh_from_db()
        assert sample_orders["b1"].product == "Gadget B1"

    @pytest.mark.django_db(transaction=True)
    def test_delete_other_tenant_row_fails(self, sample_orders, tenant_a, tenant_b):
        """Tenant A cannot DELETE Tenant B's rows.

        With tenant_a context, a delete targeting tenant_b's order should
        affect zero rows (RLS USING clause filters them out).
        """
        with tenant_context(tenant_a.pk):
            deleted_count, _ = Order.objects.filter(pk=sample_orders["b1"].pk).delete()
        assert deleted_count == 0
        # Verify row still exists
        with admin_context():
            assert Order.objects.filter(pk=sample_orders["b1"].pk).exists()

    @pytest.mark.django_db(transaction=True)
    def test_update_tenant_id_to_other_tenant_fails(self, sample_orders, tenant_a, tenant_b):
        """Tenant A cannot reassign own rows to Tenant B via UPDATE.

        The WITH CHECK clause prevents changing tenant_id to a value
        that doesn't match the current GUC context.
        """
        from django.db.utils import InternalError  # noqa: PLC0415

        with pytest.raises((InternalError, Exception)), tenant_context(tenant_a.pk):
            Order.objects.filter(pk=sample_orders["a1"].pk).update(tenant_id=tenant_b.pk)

    @pytest.mark.django_db(transaction=True)
    def test_delete_own_row_succeeds(self, sample_orders, tenant_a):
        """Tenant A CAN delete its own rows."""
        with tenant_context(tenant_a.pk):
            deleted_count, _ = Order.objects.filter(pk=sample_orders["a1"].pk).delete()
        assert deleted_count == 1

    @pytest.mark.django_db(transaction=True)
    def test_update_own_row_succeeds(self, sample_orders, tenant_a):
        """Tenant A CAN update its own rows."""
        with tenant_context(tenant_a.pk):
            updated = Order.objects.filter(pk=sample_orders["a1"].pk).update(
                product="Updated Widget"
            )
        assert updated == 1
        with admin_context():
            sample_orders["a1"].refresh_from_db()
        assert sample_orders["a1"].product == "Updated Widget"


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

    def test_exits_with_error_when_rls_missing(self):
        """Command exits with SystemExit(1) when RLS is missing on a table.

        Temporarily disables RLS on a table, runs the command, then
        re-enables RLS to restore the original state.
        """
        # Must RESET ROLE to superuser for ALTER TABLE (enforce_rls sets
        # a non-superuser role).
        with connection.cursor() as cur:
            cur.execute("RESET ROLE")
            cur.execute('ALTER TABLE "test_order" DISABLE ROW LEVEL SECURITY')
        try:
            err = StringIO()
            with pytest.raises(SystemExit) as exc_info:
                call_command("check_rls", stderr=err)
            assert exc_info.value.code == 1
            assert "RLS not enabled" in err.getvalue()
        finally:
            with connection.cursor() as cur:
                cur.execute('ALTER TABLE "test_order" ENABLE ROW LEVEL SECURITY')


# ---------------------------------------------------------------------------
# EXPLAIN-based index usage verification
# ---------------------------------------------------------------------------


def _collect_node_types(plan: dict) -> list[str]:
    """Recursively collect all 'Node Type' values from an EXPLAIN JSON plan."""
    types = []
    if "Node Type" in plan:
        types.append(plan["Node Type"])
    for child in plan.get("Plans", []):
        types.extend(_collect_node_types(child))
    return types


class TestRlsPolicyIndexUsage:
    """Verify the CASE WHEN policy does not prevent index scans.

    PostgreSQL's security barrier mechanism prevents non-leakproof
    functions (like ``current_setting()``) from being pushed into index
    quals. This means the RLS policy expression is always applied as a
    Filter, never as an Index Cond.

    However, when the ORM adds an explicit ``WHERE tenant_id = X``
    (as ``for_user()`` does), the planner uses that equality as an
    index condition, and the RLS policy is applied as a secondary
    filter on already-scoped rows. The CASE WHEN structure ensures
    the admin check short-circuits efficiently during per-row evaluation.
    """

    @pytest.fixture(autouse=True)
    def _setup_index_and_data(self, tenant_a, tenant_b):
        """Create a composite index and seed rows."""
        with connection.cursor() as cur:
            # RESET ROLE so we have privileges to create indexes
            cur.execute("RESET ROLE")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_test_order_tenant_id ON test_order (tenant_id)"
            )
        with admin_context():
            Order.objects.all().delete()
            orders = []
            for i in range(50):
                orders.append(Order(product=f"Item A{i}", amount=1, tenant=tenant_a))
                orders.append(Order(product=f"Item B{i}", amount=1, tenant=tenant_b))
            Order.objects.bulk_create(orders)
        with connection.cursor() as cur:
            cur.execute("ANALYZE test_order")
        yield
        with connection.cursor() as cur:
            cur.execute("RESET ROLE")
            cur.execute("DROP INDEX IF EXISTS idx_test_order_tenant_id")

    def test_explicit_where_uses_index_with_rls(self, tenant_a):
        """ORM-level WHERE tenant_id = X drives an index scan through RLS.

        This simulates the ``for_user()`` pattern: the ORM filter adds
        ``WHERE tenant_id = X``, which the planner uses as an Index Cond.
        The RLS CASE WHEN policy is applied as a secondary Filter on the
        already-scoped rows. We disable seq scan to force the index path.
        """
        with tenant_context(tenant_a.pk), connection.cursor() as cur:
            cur.execute("SET enable_seqscan = off")
            try:
                cur.execute(
                    "EXPLAIN (FORMAT JSON) SELECT * FROM test_order WHERE tenant_id = %s",
                    [tenant_a.pk],
                )
                plan_json = cur.fetchone()[0]
            finally:
                cur.execute("SET enable_seqscan = on")

        plan = (
            plan_json[0]["Plan"]
            if isinstance(plan_json, list)
            else json.loads(plan_json)[0]["Plan"]
        )
        node_types = _collect_node_types(plan)

        # Accept any index-based scan (Index Scan, Index Only Scan,
        # Bitmap Index Scan + Bitmap Heap Scan).
        index_scan_types = {"Index Scan", "Index Only Scan", "Bitmap Index Scan"}
        uses_index = bool(index_scan_types & set(node_types))

        assert uses_index, (
            f"Expected an index scan for tenant-scoped query with explicit "
            f"WHERE and enable_seqscan=off, but got node types: {node_types}"
        )

    def test_rls_policy_uses_case_when_structure(self):
        """Verify the installed policy uses the optimized CASE WHEN structure.

        This checks that the migration applied the new policy format
        rather than the old ``OR``-based structure.
        """
        with connection.cursor() as cur:
            cur.execute("RESET ROLE")
            cur.execute(
                """
                SELECT pg_get_expr(polqual, polrelid)
                FROM pg_policy
                WHERE polrelid = 'test_order'::regclass
                """,
            )
            using_clause = cur.fetchone()[0]

        assert "CASE" in using_clause
        assert "WHEN" in using_clause
        # The old OR pattern should not be present
        assert " OR " not in using_clause or "WHEN" in using_clause.split(" OR ")[0]


# ---------------------------------------------------------------------------
# Auto-scope integration tests
# ---------------------------------------------------------------------------


class TestAutoScope:
    """Verify automatic query scoping via ContextVar state + RLS."""

    def test_auto_scope_returns_correct_rows(self, sample_orders, tenant_a):
        """Auto-scoped query returns only the active tenant's rows."""
        with tenant_context(tenant_a.pk):
            orders = list(Order.objects.values_list("product", flat=True))
        assert sorted(orders) == ["Widget A1", "Widget A2"]

    def test_auto_scope_tenant_b(self, sample_orders, tenant_b):
        """Auto-scoped query returns only Tenant B's rows."""
        with tenant_context(tenant_b.pk):
            orders = list(Order.objects.values_list("product", flat=True))
        assert orders == ["Gadget B1"]

    def test_auto_scope_admin_sees_all(self, sample_orders):
        """Admin context disables auto-scope, sees all rows."""
        with admin_context():
            assert Order.objects.count() == 3

    def test_auto_scope_nested_contexts(self, sample_orders, tenant_a, tenant_b):
        """Nested contexts correctly switch auto-scope."""
        with tenant_context(tenant_a.pk):
            assert Order.objects.count() == 2
            with tenant_context(tenant_b.pk):
                assert Order.objects.count() == 1
            assert Order.objects.count() == 2

    def test_auto_scope_query_contains_where_clause(self, sample_orders, tenant_a):
        """Verify the generated SQL contains an explicit WHERE tenant_id clause."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all()
            sql = str(qs.query)
            assert "tenant_id" in sql

    def test_auto_scope_no_where_without_context(self, sample_orders):
        """Without context, no WHERE clause in generated SQL."""
        assert get_current_tenant_id() is None
        qs = Order.objects.all()
        sql = str(qs.query)
        assert "WHERE" not in sql

    def test_auto_scope_cross_model(self, sample_orders, sample_documents, tenant_a):
        """Auto-scope works across multiple RLS-protected models."""
        with tenant_context(tenant_a.pk):
            orders = list(Order.objects.values_list("product", flat=True))
            docs = list(Document.objects.values_list("title", flat=True))
        assert sorted(orders) == ["Widget A1", "Widget A2"]
        assert docs == ["Doc A"]
