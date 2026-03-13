"""Django application configuration for django-rls-tenants."""

from __future__ import annotations

import contextlib

from django.apps import AppConfig


class DjangoRlsTenantsConfig(AppConfig):
    """AppConfig for django-rls-tenants."""

    name = "django_rls_tenants"
    verbose_name = "Django RLS Tenants"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Connect signal handlers and validate configuration on startup."""
        from django.core.signals import request_finished  # noqa: PLC0415

        from django_rls_tenants.rls.guc import clear_guc  # noqa: PLC0415
        from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

        # Safety net: clear GUC variables when a request finishes, in case
        # the middleware's process_response didn't run (e.g., worker crash).
        def _clear_rls_on_request_finished(
            _sender: type,
            **_kwargs: object,
        ) -> None:
            try:
                conf = rls_tenants_config
                if not conf.USE_LOCAL_SET:
                    clear_guc(conf.GUC_IS_ADMIN)
                    clear_guc(conf.GUC_CURRENT_TENANT)
            except Exception:  # noqa: S110  -- safety net, connection may be closed
                pass

        request_finished.connect(_clear_rls_on_request_finished)

        # Validate configuration at startup
        with contextlib.suppress(ValueError):
            _ = rls_tenants_config.TENANT_MODEL
