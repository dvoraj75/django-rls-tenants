"""Plain views used by the middleware integration tests.

They read an RLS-protected model inside the request so the test can prove the
tenant context set by ``RLSTenantMiddleware`` is in force while the view runs.
"""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
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
from django_rls_tenants.tenants.middleware import (
    RLSTenantMiddleware,
    _were_gucs_set_on_this_thread,
)
from django_rls_tenants.tenants.selection import clear_tenant_selection, switch_tenant
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests.session_gucs import session_gucs
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


def select_tenant_view(request: HttpRequest) -> JsonResponse:
    """``POST tenant=<pk>`` selects that tenant for the session; an empty ``tenant`` clears it.

    Answers with the context the request itself ran in, which the selection does not change.
    """
    selected = request.POST.get("tenant", "")
    if selected:
        switch_tenant(request, selected)
    else:
        clear_tenant_selection(request)
    return JsonResponse(
        {
            "selection": request.session.get("rls_tenant"),
            "tenant_id": get_current_tenant_id(),
            "tenant_guc": get_guc("rls.current_tenant"),
        }
    )


def task_headers_view(request: HttpRequest) -> JsonResponse:
    """Return the RLS headers a Celery task enqueued by this request would carry."""
    from django_rls_tenants.contrib.celery import _capture  # noqa: PLC0415  -- optional extra

    return JsonResponse(_capture())


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

    Under ASGI, Django 5.2 - 6.1 build this page in a ``thread_sensitive=False``
    hop, on a worker thread with a connection of its own and a copy of the
    request's ``ContextVar``s (#130). Django 6.2 builds it on the request's
    thread-sensitive thread, with the request's connection (Django ticket
    #36027).
    """
    return JsonResponse(_guc_probe(), status=404)


def guc_probe_500_handler(request: HttpRequest) -> JsonResponse:
    """``guc_probe_404_handler`` for 500 pages."""
    return JsonResponse(_guc_probe(), status=500)


# One thread, kept for the test session, like the long-lived worker threads of an
# ASGI server's default executor. The ``*_on_worker`` handlers build their page on it.
ERROR_PAGE_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="error-page")


def _guc_probe_on_worker() -> dict[str, object]:
    """Run ``_guc_probe`` on ``ERROR_PAGE_WORKER`` with a copy of the caller's context.

    The same copy semantics as Django 5.2 - 6.1's ``thread_sensitive=False``
    error-page hop, on every Django version: Django 6.2 builds the page on the
    request's thread, and this hands it to another thread from there.
    """
    return ERROR_PAGE_WORKER.submit(contextvars.copy_context().run, _guc_probe).result()


def guc_probe_404_on_worker_handler(request: HttpRequest, exception: Exception) -> JsonResponse:
    """``guc_probe_404_handler``, with the probe run on ``ERROR_PAGE_WORKER``."""
    return JsonResponse(_guc_probe_on_worker(), status=404)


def guc_probe_500_on_worker_handler(request: HttpRequest) -> JsonResponse:
    """``guc_probe_500_handler``, with the probe run on ``ERROR_PAGE_WORKER``."""
    return JsonResponse(_guc_probe_on_worker(), status=500)


def static_500_handler(request: HttpRequest) -> JsonResponse:
    """A 500 page that runs no statement through Django's cursor, like a static ``500.html``.

    The GUC affinity guard never runs for it, so whatever is left on its
    connection afterwards is left by the request's own cleanup, or not cleared
    at all. Reports what the session held, read with a driver cursor.
    """
    session_gucs_before = list(session_gucs(connection))
    with connection.connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        (pid,) = cursor.fetchone()
    return JsonResponse(
        {
            "thread": threading.get_ident(),
            "on_request_thread": _were_gucs_set_on_this_thread(),
            "pid": pid,
            "session_gucs_before": session_gucs_before,
            "rls_context_active": get_rls_context_active(),
        },
        status=500,
    )


async def async_worker_probe_view(request: HttpRequest) -> JsonResponse:
    """Run ``_guc_probe`` in a ``thread_sensitive=False`` hop, as Django 6.1 runs error pages.

    The hop runs on a worker thread of the event loop's default executor, with a
    connection of its own and a copy of the request's ``ContextVar``s (#130), on
    every Django version.
    """
    return JsonResponse(await sync_to_async(_guc_probe, thread_sensitive=False)())


async def async_request_thread_probe_view(request: HttpRequest) -> JsonResponse:
    """Run ``_guc_probe`` in a thread-sensitive hop, as Django 6.2 runs error pages.

    The hop runs on the request's thread, with the request's connection, on
    every Django version.
    """
    return JsonResponse(await sync_to_async(_guc_probe)())


def _guc_probe() -> dict[str, object]:
    """Report this thread's connection: what the session held, then what a statement sees.

    ``session_gucs_before`` is read with a driver cursor before any statement of
    the probe's own, so the GUC affinity guard has not applied the active context
    yet: it is what the request (or an earlier one) left on the connection.
    """
    session_gucs_before = list(session_gucs(connection))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_backend_pid(), current_setting('rls.current_tenant', true), "
            "current_setting('rls.is_admin', true)"
        )
        pid, tenant_guc, is_admin_guc = cursor.fetchone()
    return {
        "thread": threading.get_ident(),
        # True on the thread that set the request's GUCs (the request's thread).
        "on_request_thread": _were_gucs_set_on_this_thread(),
        "pid": pid,
        "session_gucs_before": session_gucs_before,
        "tenant_guc": tenant_guc or None,
        "is_admin_guc": is_admin_guc or None,
        "database_products": _database_products(),
        "rls_context_active": get_rls_context_active(),
    }


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


# What ``SessionGucsAfterRequestMiddleware`` read, one ``(path, gucs)`` per request.
SESSION_GUCS_AFTER_REQUEST: list[tuple[str, list[str | None]]] = []


class SessionGucsAfterRequestMiddleware:
    """Record what the request's own connection holds once the request is done.

    Listed before ``RLSTenantMiddleware``: after the RLS middleware has cleaned
    up, it reads the session's GUCs with a driver cursor, in a thread-sensitive
    hop, so on the request's thread and connection. Under ``ASGIHandler`` that
    connection is the request's alone, and this is the only read of it.
    """

    sync_capable = False
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    async def __call__(self, request):
        response = await self.get_response(request)
        gucs = await sync_to_async(session_gucs)(connection)
        SESSION_GUCS_AFTER_REQUEST.append((request.path, list(gucs)))
        return response


class TaggingRLSTenantMiddleware(RLSTenantMiddleware):
    """A project subclass that overrides a sync hook, which keeps the hook-based path."""

    def process_response(self, request, response):
        response["X-RLS-Hooks"] = "sync"
        return super().process_response(request, response)


def exception_middleware_factory(get_response):
    """A factory's returned instance can expose hooks absent from the factory."""
    return DatabaseExceptionMiddleware(get_response)
