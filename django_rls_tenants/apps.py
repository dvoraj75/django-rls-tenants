"""Django application configuration for django-rls-tenants."""

from __future__ import annotations

import contextlib
import logging

from django.apps import AppConfig

logger = logging.getLogger("django_rls_tenants")


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
        # Only fires when the middleware actually set GUCs (thread-local flag),
        # avoiding 2 redundant DB queries on unauthenticated requests.
        def _clear_rls_on_request_finished(
            sender: type,  # noqa: ARG001  -- Django signal API
            **kwargs: object,  # noqa: ARG001  -- Django signal API
        ) -> None:
            from django_rls_tenants.tenants.middleware import (  # noqa: PLC0415
                _clear_gucs_set_flag,
                _were_gucs_set,
            )

            if not _were_gucs_set():
                return
            try:
                conf = rls_tenants_config
                if not conf.USE_LOCAL_SET:
                    clear_guc(conf.GUC_IS_ADMIN)
                    clear_guc(conf.GUC_CURRENT_TENANT)
            except Exception:
                logger.warning(
                    "Safety net: failed to clear RLS GUC variables on request_finished. "
                    "Connection may be closed or unavailable.",
                    exc_info=True,
                )
            finally:
                _clear_gucs_set_flag()

        request_finished.connect(_clear_rls_on_request_finished, weak=False)

        # Import checks module so @register decorators are activated.
        import django_rls_tenants.tenants.checks  # noqa: PLC0415, F401

        # Validate configuration at startup
        with contextlib.suppress(ValueError):
            _ = rls_tenants_config.TENANT_MODEL
