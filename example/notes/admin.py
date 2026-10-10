from django.contrib import admin

from django_rls_tenants import RLSTenantModelAdmin

from notes.models import Category, Note

# Both admins subclass ``RLSTenantModelAdmin`` instead of ``admin.ModelAdmin``.
# That is the whole integration: every admin view runs inside the RLS context of
# the logged-in user, so
#
# - the changelist is auto-scoped (no ``get_queryset`` override, no ``tenant``
#   list filter -- the manager's auto-scoping already adds ``WHERE tenant_id``),
# - the ``tenant`` FK is hidden on the add/change form and stamped on save for a
#   tenant-scoped user (``staff`` in the seeded demo),
# - a cross-tenant admin (``admin``) sees every tenant and gets a session-backed
#   tenant switcher as a changelist filter; picking a tenant scopes the views and
#   hides/auto-assigns the FK, "All" shows the field again.
#
# Related dropdowns and filters (``category`` below) are scoped the same way,
# because they query ``Category.objects`` inside the same context.


@admin.register(Category)
class CategoryAdmin(RLSTenantModelAdmin):
    list_display = ("name", "tenant", "created_at")
    search_fields = ("name",)


@admin.register(Note)
class NoteAdmin(RLSTenantModelAdmin):
    list_display = ("title", "category", "tenant", "created_at")
    # ``category`` is tenant-scoped too: the filter choices and the form dropdown
    # only list the current tenant's categories.
    list_filter = ("category",)
    search_fields = ("title", "content")
