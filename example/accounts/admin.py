from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from accounts.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "tenant", "is_tenant_admin", "is_staff")
    list_filter = ("tenant", "is_tenant_admin", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (("Tenant", {"fields": ("tenant", "is_tenant_admin")}),)
