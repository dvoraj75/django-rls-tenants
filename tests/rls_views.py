"""Plain views used by the middleware integration tests.

They read an RLS-protected model inside the request so the test can prove the
tenant context set by ``RLSTenantMiddleware`` is in force while the view runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection
from django.http import JsonResponse

from django_rls_tenants.rls.guc import get_guc
from tests.test_app.models import Order

if TYPE_CHECKING:
    from django.http import HttpRequest


def orders_view(request: HttpRequest) -> JsonResponse:
    """Return the products visible to the request's tenant plus request-time state."""
    return JsonResponse(
        {
            "products": sorted(Order.objects.values_list("product", flat=True)),
            "in_atomic_block": connection.in_atomic_block,
            "tenant_guc": get_guc("rls.current_tenant"),
            "is_admin_guc": get_guc("rls.is_admin"),
        }
    )


def boom_view(request: HttpRequest) -> JsonResponse:
    """Query inside the tenant context, then fail like a buggy view would."""
    list(Order.objects.all())
    msg = "boom"
    raise RuntimeError(msg)
