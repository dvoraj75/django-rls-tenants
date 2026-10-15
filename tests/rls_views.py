"""Plain views used by the middleware integration tests.

They read an RLS-protected model inside the request so the test can prove the
tenant context set by ``RLSTenantMiddleware`` is in force while the view runs.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import DEFAULT_DB_ALIAS, connection, connections
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseServerError,
    JsonResponse,
    StreamingHttpResponse,
)

from django_rls_tenants.rls.guc import aget_guc, get_guc, set_guc
from django_rls_tenants.tenants.context import async_admin_context, async_tenant_context
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests.test_app.models import Order, Tenant

try:
    from django.middleware import MiddlewareMixin  # Django >= 6.2
except ImportError:  # Django 5.2 - 6.1
    from django.utils.deprecation import MiddlewareMixin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django.http import HttpRequest


def _database_products(using: str = DEFAULT_DB_ALIAS) -> list[str]:
    """Read the products with raw SQL, bypassing the ORM auto-scope filter.

    Only PostgreSQL's RLS policy filters these rows.
    """
    db = connections[using]
    with db.cursor() as cursor:
        table = db.ops.quote_name(Order._meta.db_table)
        cursor.execute(f"SELECT product FROM {table} ORDER BY product")
        return [row[0] for row in cursor.fetchall()]


def orders_view(request: HttpRequest) -> JsonResponse:
    """Return the products visible to the request's tenant plus request-time state."""
    return JsonResponse(
        {
            "products": sorted(Order.objects.values_list("product", flat=True)),
            "database_products": _database_products(),
            "in_atomic_block": connection.in_atomic_block,
            "tenant_guc": get_guc("rls.current_tenant"),
            "is_admin_guc": get_guc("rls.is_admin"),
        }
    )


async def _products() -> list[str]:
    return [order.product async for order in Order.objects.order_by("product")]


async def async_orders_view(request: HttpRequest) -> JsonResponse:
    """Async twin of ``orders_view``: async ORM, raw SQL and the GUCs of the request."""
    return JsonResponse(
        {
            "products": await _products(),
            "database_products": await sync_to_async(_database_products)(),
            "tenant_id": get_current_tenant_id(),
            "tenant_guc": await aget_guc("rls.current_tenant"),
            "is_admin_guc": await aget_guc("rls.is_admin"),
        }
    )


async def async_widen_to_admin_view(request: HttpRequest) -> JsonResponse:
    """Widen the request's tenant context to admin for one block, then fall back."""
    async with async_admin_context():
        everything = await _products()
    return JsonResponse(
        {
            "admin": everything,
            "after": await _products(),
            "tenant_guc": await aget_guc("rls.current_tenant"),
        }
    )


async def _admin_rows() -> AsyncIterator[int]:
    async with async_admin_context():
        yield 1


async def async_left_open_view(request: HttpRequest) -> HttpResponse:
    """Leave an admin block open in a suspended generator, and return no response (a bug).

    Django turns the missing response into a 500 page without calling
    ``process_exception``. The request keeps the generator alive meanwhile.
    """
    rows = _admin_rows()
    await anext(rows)
    request.left_open = rows  # type: ignore[attr-defined]
    return None  # type: ignore[return-value]


async def async_stream_view(request: HttpRequest) -> StreamingHttpResponse:
    """Stream an async body; it runs after the middleware has ended the request context."""
    tenant_id = get_current_tenant_id()

    async def body() -> AsyncIterator[str]:
        yield f"{await Order.objects.acount()}\n"
        async with async_tenant_context(tenant_id):
            yield f"{await Order.objects.acount()}\n"

    return StreamingHttpResponse(body())


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


def guc_probe_404_handler(request: HttpRequest, exception: Exception) -> JsonResponse:
    """Report the context an error page runs with: its thread, connection, GUCs and rows.

    Under ASGI, Django builds this page in a ``thread_sensitive=False`` hop, on a
    worker thread with a connection of its own and a copy of the request's
    ``ContextVar``s (#130).
    """
    return _guc_probe(status=404)


def guc_probe_500_handler(request: HttpRequest) -> JsonResponse:
    """``guc_probe_404_handler`` for 500 pages."""
    return _guc_probe(status=500)


def _guc_probe(*, status: int) -> JsonResponse:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_backend_pid(), current_setting('rls.current_tenant', true), "
            "current_setting('rls.is_admin', true)"
        )
        pid, tenant_guc, is_admin_guc = cursor.fetchone()
    return JsonResponse(
        {
            "thread": threading.get_ident(),
            "pid": pid,
            "tenant_guc": tenant_guc or None,
            "is_admin_guc": is_admin_guc or None,
            "database_products": _database_products(),
            "rls_context_active": get_rls_context_active(),
        },
        status=status,
    )


def reconnect_view(request: HttpRequest) -> JsonResponse:
    """Reopen the request thread's connection, then report its GUCs and rows.

    The ``connection_created`` handler gives the new connection the request's
    GUCs, because this is the thread that set them.
    """
    connection.close()
    return JsonResponse(
        {
            "tenant_guc": get_guc("rls.current_tenant"),
            "is_admin_guc": get_guc("rls.is_admin"),
            "database_products": _database_products(),
        }
    )


def pool_probe_view(request: HttpRequest) -> JsonResponse:
    """Report the physical connection, RLS GUCs and visible rows on the ``pooled`` alias.

    ``pooled`` is registered by the ``pooled`` test fixture.
    """
    with connections["pooled"].cursor() as cursor:
        cursor.execute(
            "SELECT pg_backend_pid(), current_setting('rls.current_tenant', true), "
            "current_setting('rls.is_admin', true), "
            "current_setting('rls.is_login_request', true)"
        )
        pid, tenant, is_admin, login_flag = cursor.fetchone()
    return JsonResponse(
        {
            "pid": pid,
            # current_setting() is NULL for a GUC never set in the session, ""
            # once cleared; both mean "no context".
            "tenant_guc": tenant or None,
            "is_admin_guc": is_admin or None,
            "login_flag_guc": login_flag or None,
            "database_products": _database_products("pooled"),
        }
    )


def pool_reconnect_view(request: HttpRequest) -> JsonResponse:
    """Leave a bypass flag on the ``pooled`` connection, return it early, then probe.

    Like a view that closes its connection mid-request: the probe checks the
    same physical connection out again, and the ``connection_created`` handler
    must clear the flag and give it the request's GUCs.
    """
    set_guc("rls.is_login_request", "true", using="pooled")
    connections["pooled"].close()
    return pool_probe_view(request)


class DatabaseExceptionMiddleware(MiddlewareMixin):
    """Handle a view exception with a response that requires a database query."""

    def process_exception(self, request, exception):
        return JsonResponse({"tenants": Tenant.objects.count()}, status=409)


class SyncOnlyMiddleware:
    """A middleware that cannot run async.

    Listed after ``RLSTenantMiddleware`` under ASGI it makes Django run the
    RLS middleware on its synchronous path, adapted with ``sync_to_async``.
    """

    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class TaggingRLSTenantMiddleware(RLSTenantMiddleware):
    """A project subclass that overrides a sync hook, which keeps the hook-based path."""

    def process_response(self, request, response):
        response["X-RLS-Hooks"] = "sync"
        return super().process_response(request, response)


def exception_middleware_factory(get_response):
    """A factory's returned instance can expose hooks absent from the factory."""
    return DatabaseExceptionMiddleware(get_response)
