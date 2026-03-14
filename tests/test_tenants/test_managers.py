"""Tests for django_rls_tenants.tenants.managers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from django_rls_tenants.rls.guc import get_guc
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


class TestPrepareTenantInModelData:
    """Tests for RLSManager.prepare_tenant_in_model_data()."""

    def test_resolves_raw_id(self, tenant_a, tenant_a_user):
        """Raw tenant ID (int) is resolved to a Tenant model instance."""
        data = {"tenant": tenant_a.pk, "product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        assert isinstance(data["tenant"], Tenant)
        assert data["tenant"].pk == tenant_a.pk

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
