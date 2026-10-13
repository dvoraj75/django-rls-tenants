"""Plain views used by the middleware integration tests.

They read an RLS-protected model inside the request so the test can prove the
tenant context set by ``RLSTenantMiddleware`` is in force while the view runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection
from django.http import Http404, HttpResponse, HttpResponseServerError, JsonResponse

from django_rls_tenants.rls.guc import get_guc
from tests.test_app.models import Order, Tenant

try:
    from django.middleware import MiddlewareMixin  # Django >= 6.2
except ImportError:  # Django 5.2 - 6.1
    from django.utils.deprecation import MiddlewareMixin

if TYPE_CHECKING:
    from django.http import HttpRequest


def orders_view(request: HttpRequest) -> JsonResponse:
    """Return the products visible to the request's tenant plus request-time state."""
    # Bypass ORM auto-scoping to prove PostgreSQL itself filters the rows.
    with connection.cursor() as cursor:
        table = connection.ops.quote_name(Order._meta.db_table)
        cursor.execute(f"SELECT product FROM {table} ORDER BY product")
        database_products = [row[0] for row in cursor.fetchall()]
    return JsonResponse(
        {
            "products": sorted(Order.objects.values_list("product", flat=True)),
            "database_products": database_products,
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


def _create_order(request: HttpRequest, product: str) -> None:
    Order.objects.create(product=product, amount=1, tenant_id=request.user.rls_tenant_id)


def write_then_boom_view(request: HttpRequest) -> HttpResponse:
    """Write a row inside the tenant context, then raise (must roll back)."""
    _create_order(request, "Doomed")
    msg = "boom after write"
    raise RuntimeError(msg)


def write_then_500_view(request: HttpRequest) -> HttpResponse:
    """Write a row, then return an explicit error response (must commit)."""
    _create_order(request, "Survivor")
    return HttpResponseServerError("handled failure")


def write_then_404_view(request: HttpRequest) -> HttpResponse:
    """An application-level not-found error must roll back writes too."""
    _create_order(request, "Doomed")
    raise Http404("missing after write")


def database_404_handler(request: HttpRequest, exception: Exception) -> JsonResponse:
    """Error pages may query shared tables, like navigation or site settings."""
    return JsonResponse({"tenants": Tenant.objects.count()}, status=404)


class DatabaseExceptionMiddleware(MiddlewareMixin):
    """Handle a view exception with a response that requires a database query."""

    def process_exception(self, request, exception):
        return JsonResponse({"tenants": Tenant.objects.count()}, status=409)


def exception_middleware_factory(get_response):
    """A factory's returned instance can expose hooks absent from the factory."""
    return DatabaseExceptionMiddleware(get_response)
