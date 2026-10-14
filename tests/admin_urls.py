"""Root URLconf for the integration tests driven through ``django.test.Client``.

Exposes ``admin.site.urls`` for the ``RLSTenantModelAdmin`` tests and plain
views for the ``RLSTenantMiddleware`` request-cycle tests. Only used when
``tests.settings`` is active.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import path

from tests.rls_views import (
    async_orders_view,
    async_stream_view,
    async_widen_to_admin_view,
    boom_view,
    orders_view,
    pool_probe_view,
    pool_reconnect_view,
    reconnect_view,
    write_then_404_view,
    write_then_500_view,
    write_then_boom_view,
)

urlpatterns = [
    path("rls/write-then-404/", write_then_404_view),
    path("admin/", admin.site.urls),
    path("rls/orders/", orders_view, name="rls-orders"),
    path("rls/async-orders/", async_orders_view, name="rls-async-orders"),
    path("rls/async-widen/", async_widen_to_admin_view, name="rls-async-widen"),
    path("rls/async-stream/", async_stream_view, name="rls-async-stream"),
    path("rls/boom/", boom_view, name="rls-boom"),
    path("rls/reconnect/", reconnect_view, name="rls-reconnect"),
    path("rls/pool-probe/", pool_probe_view, name="rls-pool-probe"),
    path("rls/pool-reconnect/", pool_reconnect_view, name="rls-pool-reconnect"),
    path("rls/write-then-boom/", write_then_boom_view, name="rls-write-then-boom"),
    path("rls/write-then-500/", write_then_500_view, name="rls-write-then-500"),
]
