"""Django application configuration for django-rls-tenants."""

from __future__ import annotations

from django.apps import AppConfig


class DjangoRlsTenantsConfig(AppConfig):
    """AppConfig for django-rls-tenants."""

    name = "django_rls_tenants"
    verbose_name = "Django RLS Tenants"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Connect signal handlers on application startup."""
        # request_finished signal handler will be connected here
        # to clear GUC state and prevent cross-request leaks.
        # See plan/implementation-plan.md Phase 3.
