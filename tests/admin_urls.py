"""Root URLconf for the integration tests driven through ``django.test.Client``.

Exposes ``admin.site.urls`` for the ``RLSTenantModelAdmin`` tests and plain
views for the ``RLSTenantMiddleware`` request-cycle tests. Only used when
``tests.settings`` is active.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import path

from tests.rls_views import (
    boom_view,
    orders_view,
    write_then_404_view,
    write_then_500_view,
    write_then_boom_view,
)

urlpatterns = [
    path("rls/write-then-404/", write_then_404_view),
    path("admin/", admin.site.urls),
    path("rls/orders/", orders_view, name="rls-orders"),
    path("rls/boom/", boom_view, name="rls-boom"),
    path("rls/write-then-boom/", write_then_boom_view, name="rls-write-then-boom"),
    path("rls/write-then-500/", write_then_500_view, name="rls-write-then-500"),
]
