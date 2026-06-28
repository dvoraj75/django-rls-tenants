"""Admin registrations for the RLSTenantModelAdmin integration tests.

Auto-discovered by ``django.contrib.admin``'s ``AdminConfig.ready()`` at startup,
so these admins are registered on ``admin.site`` before the test client issues a
request.
"""

from __future__ import annotations

from django.contrib import admin

from django_rls_tenants.tenants.admin import RLSTenantModelAdmin
from tests.test_app.models import Order, OrderItem


@admin.register(Order)
class OrderAdmin(RLSTenantModelAdmin):
    """Primary admin under test (changelist, add/change/delete, switcher)."""

    list_display = ("product", "amount")
    search_fields = ("product",)  # required so Order is an autocomplete target
    ordering = ("pk",)  # deterministic changelist order (no UnorderedObjectListWarning)


@admin.register(OrderItem)
class OrderItemAdmin(RLSTenantModelAdmin):
    """Exercises an autocomplete FK to Order plus an explicit fieldsets layout.

    The ``fieldsets`` deliberately name the tenant FK so the suite covers
    ``get_fieldsets`` stripping it for implicit-tenant users (otherwise the form
    would raise ``KeyError`` at render time).
    """

    list_display = ("description",)
    search_fields = ("description",)
    autocomplete_fields = ("order",)
    ordering = ("pk",)
    fieldsets = ((None, {"fields": ("description", "order", "tenant")}),)
