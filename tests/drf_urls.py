"""URLconf for the ``django_rls_tenants.contrib.drf`` tests (``tests/drf_views.py``)."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from tests.drf_views import (
    BoomView,
    InvalidView,
    InvoiceViewSet,
    LazyProductsView,
    MemberHeaderStateView,
    MemberInvoiceViewSet,
    MemberOrderItemViewSet,
    OrderItemViewSet,
    ProtectedStateView,
    StateView,
    TenantHeaderStateView,
    TokenOnlyOpenView,
    TokenOnlyProtectedView,
    UnmixedStateView,
)

router = SimpleRouter()
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("order-items", OrderItemViewSet, basename="order-item")
router.register("member-invoices", MemberInvoiceViewSet, basename="member-invoice")
router.register("member-order-items", MemberOrderItemViewSet, basename="member-order-item")

urlpatterns = [
    path("api/", include(router.urls)),
    path("api/state/", StateView.as_view()),
    path("api/protected-state/", ProtectedStateView.as_view()),
    path("api/lazy/", LazyProductsView.as_view()),
    path("api/unmixed/", UnmixedStateView.as_view()),
    path("api/boom/", BoomView.as_view()),
    path("api/invalid/", InvalidView.as_view()),
    path("api/tenant-header/", TenantHeaderStateView.as_view()),
    path("api/member-header/", MemberHeaderStateView.as_view()),
    path("api/token-only/open/", TokenOnlyOpenView.as_view()),
    path("api/token-only/protected/", TokenOnlyProtectedView.as_view()),
]
