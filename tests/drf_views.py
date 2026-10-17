"""DRF views for the ``django_rls_tenants.contrib.drf`` tests (``tests/drf_urls.py``).

They authenticate with ``TokenAuthentication`` first, the case
``RLSTenantMiddleware`` cannot serve (DRF authenticates inside the view), and
with ``SessionAuthentication`` as well.
"""

from __future__ import annotations

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections
from rest_framework import viewsets
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django_rls_tenants.contrib.drf import (
    RLSTenantAPIViewMixin,
    RLSTenantModelSerializer,
    RLSTenantPermission,
)
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.context import tenant_context
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests.test_app.models import Invoice, Membership, Order, OrderItem

_AUTHENTICATION = (TokenAuthentication, SessionAuthentication)
_PERMISSIONS = (IsAuthenticated, RLSTenantPermission)


def database_products() -> list[str]:
    """Read the products with raw SQL: only PostgreSQL's RLS policy filters them."""
    db = connections[DEFAULT_DB_ALIAS]
    with db.cursor() as cursor:
        table = db.ops.quote_name(Order._meta.db_table)
        cursor.execute(f"SELECT product FROM {table} ORDER BY product")
        return [row[0] for row in cursor.fetchall()]


def request_state() -> dict[str, Any]:
    """The RLS state a view sees: ORM rows, raw-SQL rows, ``ContextVar``s and GUCs."""
    return {
        "products": sorted(Order.objects.values_list("product", flat=True)),
        "database_products": database_products(),
        "tenant_id": get_current_tenant_id(),
        "context_active": get_rls_context_active(),
        "tenant_guc": get_guc("rls.current_tenant"),
        "is_admin_guc": get_guc("rls.is_admin"),
    }


class InvoiceSerializer(RLSTenantModelSerializer):
    """Lists ``tenant`` so that an admin can choose it; tenant users never see it."""

    class Meta:
        model = Invoice
        fields = ("id", "number", "tenant")


class InvoiceViewSet(RLSTenantAPIViewMixin, viewsets.ModelViewSet):
    """CRUD on ``Invoice`` (per-tenant unique ``number``)."""

    authentication_classes = _AUTHENTICATION
    permission_classes = _PERMISSIONS
    serializer_class = InvoiceSerializer

    def get_queryset(self):
        return Invoice.objects.order_by("number")


class OrderItemSerializer(RLSTenantModelSerializer):
    """Its ``order`` field lists the tenant's orders in the browsable API's form."""

    class Meta:
        model = OrderItem
        fields = ("id", "order", "description")


class OrderItemViewSet(RLSTenantAPIViewMixin, viewsets.ModelViewSet):
    """CRUD on ``OrderItem``; the browsable API renders a form with an ``order`` dropdown."""

    authentication_classes = _AUTHENTICATION
    permission_classes = _PERMISSIONS
    serializer_class = OrderItemSerializer

    def get_queryset(self):
        return OrderItem.objects.order_by("description")


class StateView(RLSTenantAPIViewMixin, APIView):
    """Return what the view sees: rows, ``ContextVar``s and GUCs."""

    authentication_classes = _AUTHENTICATION
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response(request_state())


class ProtectedStateView(StateView):
    """``StateView`` behind ``RLSTenantPermission``, with the default ``get_rls_context()``.

    A ``MultiTenantUser`` (the test ``AdminUser``) selects a tenant with the library's
    ``X-Tenant`` header and session selection (#47).
    """

    permission_classes = _PERMISSIONS


class LazyProductsView(RLSTenantAPIViewMixin, APIView):
    """Return an unevaluated queryset: DRF's encoder evaluates it while rendering."""

    authentication_classes = _AUTHENTICATION
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response(Order.objects.order_by("product").values_list("product", flat=True))


class UnmixedStateView(APIView):
    """A view that forgot the mixin; ``RLSTenantPermission`` must refuse token clients."""

    authentication_classes = _AUTHENTICATION
    permission_classes = _PERMISSIONS

    def get(self, request):
        return Response(request_state())


class BoomView(RLSTenantAPIViewMixin, APIView):
    """Raise an error DRF does not handle (a 500) inside the context."""

    authentication_classes = _AUTHENTICATION
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        list(Order.objects.all())
        msg = "boom"
        raise RuntimeError(msg)


class InvalidView(RLSTenantAPIViewMixin, APIView):
    """Raise an error DRF turns into a 400 inside the context."""

    authentication_classes = _AUTHENTICATION
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        list(Order.objects.all())
        raise ValidationError({"detail": "invalid"})


class TenantHeaderStateView(StateView):
    """Lets a tenant admin pick a tenant with the ``X-Tenant`` header."""

    def get_rls_context(self, request):
        selected = request.headers.get("X-Tenant")
        if selected and request.user.is_tenant_admin:
            return tenant_context(int(selected))
        return super().get_rls_context(request)


class MemberHeaderMixin(RLSTenantAPIViewMixin):
    """Lets any user pick one of their tenants with the ``X-Tenant`` header (#150)."""

    permission_classes = _PERMISSIONS

    def get_rls_context(self, request):
        selected = request.headers.get("X-Tenant")
        if selected is None or not request.user.is_authenticated:
            return super().get_rls_context(request)
        tenant_id = int(selected)
        if not Membership.objects.filter(user=request.user, tenant_id=tenant_id).exists():
            msg = "You are not a member of the selected tenant."
            raise PermissionDenied(msg)
        return tenant_context(tenant_id)


class MemberHeaderStateView(MemberHeaderMixin, StateView):
    """``StateView`` with the ``X-Tenant`` selection."""


class MemberInvoiceViewSet(MemberHeaderMixin, InvoiceViewSet):
    """``InvoiceViewSet`` with the ``X-Tenant`` selection: writes and object lookups."""


class MemberOrderItemViewSet(MemberHeaderMixin, OrderItemViewSet):
    """``OrderItemViewSet`` with the ``X-Tenant`` selection: the browsable API's form."""


class TokenOnlyOpenView(RLSTenantAPIViewMixin, APIView):
    """Accepts tokens only: DRF leaves a session-cookie request anonymous."""

    authentication_classes = (TokenAuthentication,)
    permission_classes = (AllowAny,)

    def get(self, request):
        return Response(
            {
                "user_authenticated": bool(request.user and request.user.is_authenticated),
                "context_active": get_rls_context_active(),
            }
        )


class TokenOnlyProtectedView(TokenOnlyOpenView):
    """As ``TokenOnlyOpenView``, refused by ``RLSTenantPermission``."""

    permission_classes = _PERMISSIONS
