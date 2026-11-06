"""Django Ninja APIs for the ``django_rls_tenants.contrib.ninja`` tests (``tests/ninja_urls.py``).

Every ``NinjaAPI`` instance can be mounted once per process (its ``urls_namespace`` must
be unique), so each API below has its own namespace. They authenticate with an API key
(``X-User`` header: the user's username), the case ``RLSTenantMiddleware`` cannot serve:
Ninja authenticates inside the operation, after every middleware.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async
from django.db import DEFAULT_DB_ALIAS, connections
from ninja import JSONL, NinjaAPI, Router, Schema
from ninja.security import APIKeyHeader

from django_rls_tenants.contrib.ninja import (
    RLSTenantGuard,
    RLSTenantModelSchema,
    RLSTenantNinjaAPI,
    arls_save,
    rls_save,
)
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.context import (
    admin_context,
    async_tenant_context,
    tenant_context,
)
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    get_rls_context_active,
)
from tests.test_app.models import AdminUser, Order, OrderItem

# Where each operation saw things, for tests that check the order of the steps.
EVENTS: list[tuple[str, Any]] = []


class UserKey(APIKeyHeader):
    """Authenticate with ``X-User: <username>``; records the context it runs in."""

    param_name = "X-User"

    def authenticate(self, request, key):
        EVENTS.append(("auth", get_current_tenant_id()))
        return AdminUser.objects.filter(username=key).first()


def database_products() -> list[str]:
    """Read the products with raw SQL: only PostgreSQL's RLS policy filters them."""
    db = connections[DEFAULT_DB_ALIAS]
    with db.cursor() as cursor:
        table = db.ops.quote_name(Order._meta.db_table)
        cursor.execute(f"SELECT product FROM {table} ORDER BY product")
        return [row[0] for row in cursor.fetchall()]


def request_state() -> dict[str, Any]:
    """The RLS state a synchronous view sees: ORM rows, raw-SQL rows, ``ContextVar``s, GUCs."""
    return {
        "products": sorted(Order.objects.values_list("product", flat=True)),
        "database_products": database_products(),
        "tenant_id": get_current_tenant_id(),
        "context_active": get_rls_context_active(),
        "tenant_guc": get_guc("rls.current_tenant"),
        "is_admin_guc": get_guc("rls.is_admin"),
    }


async def arequest_state() -> dict[str, Any]:
    """The same for an ``async def`` view: the ORM rows come from the async ORM."""
    state = await sync_to_async(request_state)()
    state["products"] = sorted([p async for p in Order.objects.values_list("product", flat=True)])
    return state


class OrderOut(RLSTenantModelSchema):
    """``Order`` without its tenant; ``seen_tenant`` is read while Ninja serializes."""

    seen_tenant: int | None = None

    class Meta:
        model = Order
        fields = "__all__"

    @staticmethod
    def resolve_seen_tenant(obj):
        EVENTS.append(("serialize", get_current_tenant_id()))
        return get_current_tenant_id()


class ItemIn(RLSTenantModelSchema):
    """An ``OrderItem`` payload: no ``id``, and the tenant is never part of it."""

    class Meta:
        model = OrderItem
        exclude = ["id"]  # noqa: RUF012  -- Ninja Meta convention


class TenantSneakyIn(Schema):
    """A plain schema that lets a client name the tenant; ``rls_save()`` must drop it."""

    description: str
    order_id: int
    tenant_id: int | None = None
    tenant: int | None = None


class ItemPatch(Schema):
    """A partial update payload."""

    description: str | None = None
    order_id: int | None = None
    tenant_id: int | None = None


class BoomError(Exception):
    """Raised by a synchronous operation; ``api`` renders it with the DB state."""


class ABoomError(Exception):
    """Raised by an ``async def`` operation; its handler reads ``ContextVar``s only."""


guard = RLSTenantGuard()

api = RLSTenantNinjaAPI(auth=UserKey(), rls_guard=guard, urls_namespace="rls_ninja")


@api.exception_handler(BoomError)
def _render_boom(request, exc):
    return api.create_response(request, {"error": "boom", **request_state()}, status=418)


@api.exception_handler(ABoomError)
def _render_aboom(request, exc):
    return api.create_response(
        request,
        {
            "error": "aboom",
            "tenant_id": get_current_tenant_id(),
            "context_active": get_rls_context_active(),
        },
        status=418,
    )


@api.get("/state")
def state(request):
    return request_state()


@api.get("/astate")
async def astate(request):
    return await arequest_state()


@api.get("/lazy", response=list[OrderOut])
def lazy(request):
    EVENTS.append(("view", get_current_tenant_id()))
    return Order.objects.order_by("product")


@api.get("/alazy", response=list[OrderOut])
async def alazy(request):
    # Ninja serializes in the event loop, where an unevaluated queryset would raise
    # SynchronousOnlyOperation: fetch the rows, and let the schema read the context.
    EVENTS.append(("view", get_current_tenant_id()))
    return [order async for order in Order.objects.order_by("product")]


@api.get("/boom")
def boom(request):
    list(Order.objects.all())
    raise BoomError


@api.get("/aboom")
async def aboom(request):
    raise ABoomError


@api.get("/crash")
def crash(request):
    list(Order.objects.all())
    msg = "unhandled"
    raise RuntimeError(msg)


@api.get("/acrash")
async def acrash(request):
    msg = "unhandled"
    raise RuntimeError(msg)


@api.post("/items")
def create_item(request, payload: ItemIn):
    item = rls_save(request, OrderItem(), payload)
    return {"id": item.pk, "tenant_id": item.tenant_id, "order_id": item.order_id}


@api.post("/aitems")
async def acreate_item(request, payload: ItemIn):
    item = await arls_save(request, OrderItem(), payload)
    return {"id": item.pk, "tenant_id": item.tenant_id, "order_id": item.order_id}


@api.post("/sneaky-items")
def create_sneaky_item(request, payload: TenantSneakyIn):
    item = rls_save(request, OrderItem(), payload)
    return {"id": item.pk, "tenant_id": item.tenant_id}


@api.patch("/items/{item_id}")
def patch_item(request, item_id: int, payload: ItemPatch):
    item = rls_save(request, OrderItem.objects.get(pk=item_id), payload, exclude_unset=True)
    return {
        "id": item.pk,
        "description": item.description,
        "order_id": item.order_id,
        "tenant_id": item.tenant_id,
    }


@api.get("/other-context")
def other_context(request):
    """Run the guard inside a tenant context some other code entered (``?tenant=<pk>``)."""
    with tenant_context(int(request.GET["tenant"])):
        return {"allowed": guard.has_permission(request)}


@api.get("/object-permission/{order_id}")
def object_permission(request, order_id: int):
    order = Order.objects.get(pk=order_id)
    return {"allowed": guard.has_object_permission(request, order)}


# Routers mounted on the API, one nested in the other.
outer = Router()
inner = Router()


@outer.get("/state")
def outer_state(request):
    return request_state()


@inner.get("/state")
def inner_state(request):
    return request_state()


@inner.get("/astate")
async def inner_astate(request):
    return await arequest_state()


outer.add_router("/inner", inner)
api.add_router("/outer", outer)


# An API that picks the tenant itself, without validating it: the guard accepts the
# tenant context the API entered, whichever tenant that is (#150).
class PickAPI(RLSTenantNinjaAPI):
    def get_rls_context(self, request):
        return tenant_context(int(request.headers["X-Pick"]))

    async def aget_rls_context(self, request):
        return async_tenant_context(int(request.headers["X-Pick"]))


pick_api = PickAPI(auth=UserKey(), rls_guard=RLSTenantGuard(), urls_namespace="rls_ninja_pick")


@pick_api.get("/state")
def pick_state(request):
    return request_state()


@pick_api.get("/astate")
async def pick_astate(request):
    return await arequest_state()


# An API that hands every user the admin bypass: the guard refuses it for a non-admin.
class AdminForAllAPI(RLSTenantNinjaAPI):
    def get_rls_context(self, request):
        return admin_context()


admin_for_all_api = AdminForAllAPI(
    auth=UserKey(), rls_guard=RLSTenantGuard(), urls_namespace="rls_ninja_admin_for_all"
)


@admin_for_all_api.get("/state")
def admin_for_all_state(request):
    return request_state()


# A plain NinjaAPI: nothing enters a context, so only the guard protects the operation.
plain_api = NinjaAPI(auth=UserKey(), urls_namespace="rls_ninja_plain")


@plain_api.get("/guarded")
@guard
def plain_guarded(request):
    return request_state()


@plain_api.get("/unguarded")
def plain_unguarded(request):
    return request_state()


# No ``auth=``: the user is ``request.user`` (the session), or nobody.
open_api = RLSTenantNinjaAPI(urls_namespace="rls_ninja_open")


@open_api.get("/state")
def open_state(request):
    return request_state()


@open_api.get("/guarded")
@guard
def open_guarded(request):
    return request_state()


# Streaming operations get no context (documented limitation).
stream_api = RLSTenantNinjaAPI(auth=UserKey(), urls_namespace="rls_ninja_stream")


@stream_api.get("/stream", response=JSONL[dict])
def stream(request):
    yield {"tenant_id": get_current_tenant_id(), "context_active": get_rls_context_active()}


@stream_api.get("/astream", response=JSONL[dict])
async def astream(request):
    yield {"tenant_id": get_current_tenant_id(), "context_active": get_rls_context_active()}
