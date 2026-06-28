"""Root URLconf for the admin integration tests.

Exposes ``admin.site.urls`` so ``django.test.Client`` can drive the
``RLSTenantModelAdmin`` views. Only used when ``tests.settings`` is active.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]
