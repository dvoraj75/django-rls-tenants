"""URLconf for the ``django_rls_tenants.contrib.ninja`` tests (``tests/ninja_views.py``)."""

from __future__ import annotations

from django.urls import path

from tests.ninja_views import (
    admin_for_all_api,
    api,
    guarded_stream_api,
    key_api,
    open_api,
    pick_api,
    plain_api,
    stream_api,
)

urlpatterns = [
    path("api/", api.urls),
    path("pick/", pick_api.urls),
    path("admin-for-all/", admin_for_all_api.urls),
    path("plain/", plain_api.urls),
    path("open/", open_api.urls),
    path("stream/", stream_api.urls),
    path("guarded-stream/", guarded_stream_api.urls),
    path("key/", key_api.urls),
]
