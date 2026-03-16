"""Tests for django_rls_tenants.tenants.managers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.managers import _is_rls_protected
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    reset_current_tenant_id,
    set_current_tenant_id,
)
from tests.test_app.models import Order, Tenant

pytestmark = pytest.mark.django_db


class TestForUserTenantUser:
    """Tests for TenantQuerySet.for_user() with tenant users."""

    def test_returns_only_tenant_rows(self, sample_orders, tenant_a_user):
        """Tenant user sees only their own tenant's rows."""
        qs = Order.objects.for_user(tenant_a_user)
        products = list(qs.values_list("product", flat=True))
        assert sorted(products) == ["Widget A1", "Widget A2"]

    def test_does_not_return_other_tenant_rows(self, sample_orders, tenant_b_user):
        """Tenant B user does not see Tenant A's rows."""
        qs = Order.objects.for_user(tenant_b_user)
        products = list(qs.values_list("product", flat=True))
        assert products == ["Gadget B1"]


class TestForUserAdmin:
    """Tests for TenantQuerySet.for_user() with admin users."""

    def test_returns_all_rows(self, sample_orders, admin_user):
        """Admin user sees all tenants' rows."""
        qs = Order.objects.for_user(admin_user)
        assert qs.count() == 3


class TestForUserLazy:
    """Tests for lazy evaluation behavior."""

    def test_chainable(self, sample_orders, tenant_a_user):
        """for_user() result is chainable with standard queryset methods."""
        qs = Order.objects.for_user(tenant_a_user).order_by("product")
        products = list(qs.values_list("product", flat=True))
        assert products == ["Widget A1", "Widget A2"]

    def test_filter_after_for_user(self, sample_orders, tenant_a_user):
        """filter() after for_user() preserves the user reference."""
        qs = Order.objects.for_user(tenant_a_user).filter(amount__gte=Decimal("15.00"))
        products = list(qs.values_list("product", flat=True))
        assert products == ["Widget A2"]

    def test_lazy_evaluation(self, sample_orders, tenant_a_user):
        """QuerySet from for_user() works when evaluated later (lazy).

        This is the critical test for Decision 1 (RFC comment 1.2):
        GUC must NOT be set when for_user() is called. It must be set
        only at evaluation time, inside _fetch_all().
        """
        # Create queryset (no SQL executed yet)
        qs = Order.objects.for_user(tenant_a_user)
        # GUC should NOT be set yet -- for_user() only stores the user
        assert get_guc("rls.current_tenant") is None
        # Force evaluation
        count = qs.count()
        assert count == 2
        # GUC should be cleared after evaluation
        assert get_guc("rls.current_tenant") is None

    def test_clone_propagates_user(self, sample_orders, tenant_a_user):
        """Cloned querysets (from .filter(), .order_by()) keep the user ref."""
        qs1 = Order.objects.for_user(tenant_a_user)
        qs2 = qs1.filter(product="Widget A1")
        assert qs2._rls_user is tenant_a_user
        assert list(qs2.values_list("product", flat=True)) == ["Widget A1"]


class TestFetchAllGucCleanupOnException:
    """Tests that GUC variables are cleaned up even when _fetch_all raises."""

    def test_guc_cleared_after_db_error(self, tenant_a_user, monkeypatch):
        """GUC variables are cleared even when the underlying query raises.

        This verifies the try/finally in _fetch_all properly cleans up
        GUC state on failure, preventing stale tenant context.
        """
        qs = Order.objects.for_user(tenant_a_user)

        def exploding_fetch_all(self_inner):
            raise RuntimeError("Simulated DB error")

        import django.db.models.query  # noqa: PLC0415

        monkeypatch.setattr(django.db.models.query.QuerySet, "_fetch_all", exploding_fetch_all)

        with pytest.raises(RuntimeError, match="Simulated DB error"):
            list(qs)  # trigger _fetch_all

        # GUC should be cleared despite the error
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None


class TestAutoScope:
    """Tests for automatic query scoping via ContextVar state."""

    def test_auto_scope_adds_filter_in_tenant_context(self, sample_orders, tenant_a):
        """get_queryset() adds tenant filter when tenant context is active."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all()
            # The queryset should contain a tenant_id filter
            sql = str(qs.query)
            assert "tenant_id" in sql
            products = list(qs.values_list("product", flat=True))
            assert sorted(products) == ["Widget A1", "Widget A2"]

    def test_auto_scope_no_filter_without_context(self, sample_orders):
        """get_queryset() adds no filter when no context is active."""
        assert get_current_tenant_id() is None
        qs = Order.objects.all()
        sql = str(qs.query)
        assert "WHERE" not in sql

    def test_auto_scope_no_filter_in_admin_context(self, sample_orders):
        """get_queryset() adds no filter when admin context is active."""
        with admin_context():
            qs = Order.objects.all()
            sql = str(qs.query)
            assert "WHERE" not in sql

    def test_auto_scope_with_chained_filter(self, sample_orders, tenant_a):
        """Auto-scope works with additional .filter() calls."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.filter(amount__gte=Decimal("15.00"))
            products = list(qs.values_list("product", flat=True))
            assert products == ["Widget A2"]

    def test_auto_scope_changes_on_context_switch(self, sample_orders, tenant_a, tenant_b):
        """Auto-scope follows the active tenant context."""
        with tenant_context(tenant_a.pk):
            a_products = sorted(Order.objects.values_list("product", flat=True))
            assert a_products == ["Widget A1", "Widget A2"]

        with tenant_context(tenant_b.pk):
            b_products = list(Order.objects.values_list("product", flat=True))
            assert b_products == ["Gadget B1"]

    def test_for_user_still_works_with_auto_scope(self, sample_orders, tenant_a_user, tenant_a):
        """for_user() still works correctly even when auto-scope is also active.

        Both add a WHERE tenant_id = X clause. PostgreSQL deduplicates them.
        """
        with tenant_context(tenant_a.pk):
            qs = Order.objects.for_user(tenant_a_user)
            products = sorted(qs.values_list("product", flat=True))
            assert products == ["Widget A1", "Widget A2"]

    def test_auto_scope_with_manual_state(self, sample_orders, tenant_a):
        """Auto-scope works when state is set manually (not via context manager)."""
        token = set_current_tenant_id(tenant_a.pk)
        try:
            qs = Order.objects.all()
            sql = str(qs.query)
            assert "tenant_id" in sql
        finally:
            reset_current_tenant_id(token)


class TestFetchAllContextVar:
    """Tests for ContextVar management in _fetch_all()."""

    def test_fetch_all_sets_contextvar_for_tenant_user(self, sample_orders, tenant_a_user):
        """_fetch_all() sets ContextVar during evaluation for prefetch support."""
        qs = Order.objects.for_user(tenant_a_user)
        # Before evaluation, ContextVar should be None
        assert get_current_tenant_id() is None
        # Evaluate
        list(qs)
        # After evaluation, ContextVar should be restored to None
        assert get_current_tenant_id() is None

    def test_fetch_all_clears_contextvar_for_admin(self, sample_orders, admin_user):
        """_fetch_all() sets ContextVar to None for admin users."""
        qs = Order.objects.for_user(admin_user)
        assert get_current_tenant_id() is None
        list(qs)
        assert get_current_tenant_id() is None

    def test_fetch_all_restores_contextvar_on_exception(self, tenant_a_user, monkeypatch):
        """ContextVar is restored even when _fetch_all raises."""
        qs = Order.objects.for_user(tenant_a_user)

        import django.db.models.query  # noqa: PLC0415

        def exploding_fetch(self_inner):
            # Verify ContextVar IS set during evaluation
            assert get_current_tenant_id() == tenant_a_user.rls_tenant_id
            raise RuntimeError("boom")

        monkeypatch.setattr(django.db.models.query.QuerySet, "_fetch_all", exploding_fetch)

        with pytest.raises(RuntimeError, match="boom"):
            list(qs)

        # Must be restored after exception
        assert get_current_tenant_id() is None


class TestGetActiveTenantId:
    """Tests for TenantQuerySet._get_active_tenant_id()."""

    def test_returns_none_without_context(self, sample_orders):
        """Returns None when no tenant context is active."""
        qs = Order.objects.all()
        assert qs._get_active_tenant_id() is None

    def test_returns_contextvar_value(self, sample_orders, tenant_a):
        """Returns ContextVar value when tenant context is active."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all()
            assert qs._get_active_tenant_id() == tenant_a.pk

    def test_returns_rls_user_tenant_id(self, sample_orders, tenant_a_user):
        """Returns _rls_user.rls_tenant_id when for_user() is used."""
        qs = Order.objects.for_user(tenant_a_user)
        assert qs._get_active_tenant_id() == tenant_a_user.rls_tenant_id

    def test_returns_none_for_admin_user(self, sample_orders, admin_user):
        """Returns None for admin users (no filter should be applied)."""
        qs = Order.objects.for_user(admin_user)
        assert qs._get_active_tenant_id() is None

    def test_contextvar_takes_precedence(self, sample_orders, tenant_a_user, tenant_b):
        """ContextVar takes precedence over _rls_user."""
        qs = Order.objects.for_user(tenant_a_user)
        with tenant_context(tenant_b.pk):
            # ContextVar (tenant_b) should take precedence over _rls_user (tenant_a)
            assert qs._get_active_tenant_id() == tenant_b.pk


class TestIsRlsProtected:
    """Tests for the _is_rls_protected() helper."""

    def test_order_is_protected(self):
        """Order model (RLSProtectedModel) is detected as RLS-protected."""
        assert _is_rls_protected(Order) is True

    def test_tenant_is_not_protected(self):
        """Tenant model (plain model) is not RLS-protected."""
        assert _is_rls_protected(Tenant) is False


class TestPrepareTenantInModelData:
    """Tests for RLSManager.prepare_tenant_in_model_data()."""

    def test_resolves_raw_id(self, tenant_a, tenant_a_user):
        """Raw tenant ID (int) sets FK column directly (no model fetch)."""
        data = {"tenant": tenant_a.pk, "product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        # The FK field is replaced by the FK column (tenant -> tenant_id)
        assert "tenant" not in data
        assert data["tenant_id"] == tenant_a.pk

    def test_skips_model_instance(self, tenant_a, tenant_a_user):
        """Already a Tenant instance -- no change."""
        data = {"tenant": tenant_a, "product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        assert data["tenant"] is tenant_a

    def test_skips_missing_key(self, tenant_a_user):
        """No tenant key in data -- no error."""
        data = {"product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        assert "tenant" not in data
