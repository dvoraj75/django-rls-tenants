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
        from django.db.backends.signals import connection_created  # noqa: PLC0415

        from django_rls_tenants.rls.guc import clear_guc, set_guc  # noqa: PLC0415
        from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

        # Safety net: clear GUC variables when a request finishes, in case
        # the middleware's process_response didn't run (e.g., worker crash).
        # Only fires when the middleware actually set GUCs (ContextVar flag),
        # avoiding redundant DB queries on unauthenticated requests.
        # Iterates over all configured DATABASES to clear GUCs on every alias.
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
                from django_rls_tenants.tenants.state import (  # noqa: PLC0415
                    set_current_tenant_id,
                )

                set_current_tenant_id(None)
                conf = rls_tenants_config
                if not conf.USE_LOCAL_SET:
                    for db_alias in conf.DATABASES:
                        clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
                        clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
            except Exception:
                logger.warning(
                    "Safety net: failed to clear RLS GUC variables on request_finished. "
                    "Connection may be closed or unavailable.",
                    exc_info=True,
                )
            finally:
                _clear_gucs_set_flag()

        request_finished.connect(_clear_rls_on_request_finished, weak=False)

        # Signal handler for lazily created database connections.
        # When a new connection is established mid-request on a configured
        # database alias, reads the current tenant state from ContextVars
        # and sets GUCs on the new connection. This covers the case where
        # a replica connection is lazily created after the middleware has
        # already run on eagerly-opened connections.
        def _set_guc_on_new_connection(
            sender: type,  # noqa: ARG001  -- Django signal API
            connection: object,
            **kwargs: object,  # noqa: ARG001  -- Django signal API
        ) -> None:
            from django_rls_tenants.tenants.middleware import _were_gucs_set  # noqa: PLC0415
            from django_rls_tenants.tenants.state import (  # noqa: PLC0415
                get_current_tenant_id,
            )

            db_alias: str = getattr(connection, "alias", "")
            conf = rls_tenants_config

            if db_alias not in conf.DATABASES:
                return

            if not _were_gucs_set():
                return  # No active request context

            # Read tenant state from ContextVar and apply GUCs.
            tenant_id = get_current_tenant_id()
            try:
                if tenant_id is not None:
                    # Tenant context: set tenant ID and mark as non-admin
                    set_guc(
                        conf.GUC_CURRENT_TENANT,
                        str(tenant_id),
                        is_local=conf.USE_LOCAL_SET,
                        using=db_alias,
                    )
                    set_guc(
                        conf.GUC_IS_ADMIN,
                        "false",
                        is_local=conf.USE_LOCAL_SET,
                        using=db_alias,
                    )
                else:
                    # Admin context (tenant_id is None but GUCs were set)
                    clear_guc(conf.GUC_CURRENT_TENANT, is_local=conf.USE_LOCAL_SET, using=db_alias)
                    set_guc(
                        conf.GUC_IS_ADMIN,
                        "true",
                        is_local=conf.USE_LOCAL_SET,
                        using=db_alias,
                    )
            except Exception:
                logger.warning(
                    "Failed to set RLS GUCs on new connection for alias %r. "
                    "Queries on this connection may return empty results.",
                    db_alias,
                    exc_info=True,
                )

        connection_created.connect(_set_guc_on_new_connection, weak=False)

        # Import checks module so @register decorators are activated.
        import django_rls_tenants.tenants.checks  # noqa: PLC0415, F401

        # Validate configuration at startup
        from django_rls_tenants.exceptions import RLSConfigurationError  # noqa: PLC0415

        with contextlib.suppress(RLSConfigurationError):
            _ = rls_tenants_config.TENANT_MODEL
