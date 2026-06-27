"""Integration tests for the raw-SQL helpers (issue #33).

These require a live PostgreSQL database with RLS policies applied and run under
the non-superuser ``rls_test_role`` (RLS enforced), via the ``enforce_rls``
fixture auto-applied in this package's conftest.

They prove the ``safe_tenant_sql`` fragment actually scopes a raw query to the
current tenant -- including independently of RLS, by running under
``admin_context()`` (which bypasses RLS) and showing the fragment alone still
filters -- and that ``current_tenant_value_sql`` yields the active tenant id.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection

from django_rls_tenants.rls.guc import set_guc
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.sql import current_tenant_value_sql, safe_tenant_sql

pytestmark = pytest.mark.django_db


class TestSafeTenantSqlFragment:
    """safe_tenant_sql() scopes raw queries to the current tenant."""

    def test_fragment_scopes_to_current_tenant(self, sample_orders, tenant_a):
        """Under tenant_context, the fragment returns only that tenant's rows."""
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute(f"SELECT product FROM test_order WHERE {safe_tenant_sql()}")
            products = sorted(row[0] for row in cursor.fetchall())
        assert products == ["Widget A1", "Widget A2"]

    def test_admin_sees_all_via_admin_branch(self, sample_orders):
        """include_admin=True lets an admin_context query see every tenant's rows."""
        with admin_context(), connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM test_order WHERE {safe_tenant_sql()}")
            count = cursor.fetchone()[0]
        assert count == 3

    def test_include_admin_false_scopes_even_under_admin(self, sample_orders, tenant_a):
        """The fragment itself filters, independently of RLS.

        admin_context() bypasses RLS (all rows are visible at the policy level),
        yet ``safe_tenant_sql(include_admin=False)`` with the tenant GUC set to A
        still returns only A's rows -- proving the WHERE fragment does the
        scoping, not the policy.
        """
        with admin_context(), connection.cursor() as cursor:
            set_guc("rls.current_tenant", str(tenant_a.pk))
            cursor.execute(
                f"SELECT product FROM test_order WHERE {safe_tenant_sql(include_admin=False)}"
            )
            products = sorted(row[0] for row in cursor.fetchall())
        assert products == ["Widget A1", "Widget A2"]

    def test_include_admin_false_fail_closed_without_tenant(self, sample_orders):
        """With no tenant set, the bare fragment matches nothing (fail-closed).

        Even under admin_context() (RLS bypassed), an empty current_tenant GUC
        makes the predicate ``tenant_id = NULL`` -- so zero rows match.
        """
        with admin_context(), connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM test_order WHERE {safe_tenant_sql(include_admin=False)}"
            )
            count = cursor.fetchone()[0]
        assert count == 0

    def test_table_qualified_fragment(self, sample_orders, tenant_a):
        """A table-qualified fragment works in a real query."""
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute(
                f"SELECT product FROM test_order WHERE {safe_tenant_sql(table='test_order')}"
            )
            products = sorted(row[0] for row in cursor.fetchall())
        assert products == ["Widget A1", "Widget A2"]

    def test_table_qualified_fragment_with_admin_context(self, sample_orders):
        """A table-qualified fragment composes with the admin branch in a live query."""
        with admin_context(), connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM test_order WHERE {safe_tenant_sql(table='test_order')}"
            )
            count = cursor.fetchone()[0]
        assert count == 3

    def test_fragment_composes_safely_with_trailing_and(self, sample_orders, tenant_a):
        """The parenthesized fragment composes correctly with a trailing AND filter.

        Tenant A owns Widget A1 (10.00) and Widget A2 (20.00); ``AND amount < 15``
        must exclude A2. Were the fragment's outer parens dropped, OR/AND
        precedence would turn ``... OR is_admin AND amount < 15`` into
        ``tenant_match OR (is_admin AND amount < 15)`` and let A2 slip through.
        """
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute(
                f"SELECT product FROM test_order WHERE {safe_tenant_sql()} AND amount < %s",
                [15],
            )
            products = sorted(row[0] for row in cursor.fetchall())
        assert products == ["Widget A1"]

    def test_extra_bypass_flag_matches_policy_bypass(self, sample_protected_users):
        """extra_bypass_flags mirrors the live policy; omitting it diverges.

        ``ProtectedUser`` carries ``extra_bypass_flags=["rls.is_login_request",
        ...]``. With that GUC set (and no tenant/admin context), the RLS policy
        makes every row visible. The flag-aware fragment matches them all; the
        flag-unaware fragment -- now out of step with the policy -- matches none.
        """
        flag_aware = safe_tenant_sql(extra_bypass_flags=["rls.is_login_request"])
        flag_unaware = safe_tenant_sql()
        with connection.cursor() as cursor:
            set_guc("rls.is_login_request", "true")
            cursor.execute(f"SELECT count(*) FROM test_protected_user WHERE {flag_aware}")
            with_flag = cursor.fetchone()[0]
            cursor.execute(f"SELECT count(*) FROM test_protected_user WHERE {flag_unaware}")
            without_flag = cursor.fetchone()[0]
        assert with_flag == 2
        assert without_flag == 0


class TestCurrentTenantValueSqlFragment:
    """current_tenant_value_sql() yields the active tenant id in raw SQL."""

    def test_returns_active_tenant_id(self, tenant_a):
        """SELECTing the value expression returns the current tenant's id."""
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute(f"SELECT {current_tenant_value_sql()}")
            value = cursor.fetchone()[0]
        assert value == tenant_a.pk

    def test_null_without_context(self):
        """With no tenant context the value expression is NULL."""
        with admin_context(), connection.cursor() as cursor:
            cursor.execute(f"SELECT {current_tenant_value_sql()}")
            value = cursor.fetchone()[0]
        assert value is None

    def test_usable_as_insert_value(self, tenant_a):
        """The value expression supplies tenant_id on a raw INSERT.

        Under tenant_context the inserted row's tenant_id matches the GUC, so it
        also satisfies the RLS WITH CHECK clause.
        """
        with tenant_context(tenant_a.pk), connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO test_order (product, amount, tenant_id) "
                f"VALUES (%s, %s, {current_tenant_value_sql()})",
                ["Raw Widget", Decimal("9.99")],
            )
        with admin_context():
            from tests.test_app.models import Order  # noqa: PLC0415

            inserted = Order.objects.get(product="Raw Widget")
        assert inserted.tenant_id == tenant_a.pk
