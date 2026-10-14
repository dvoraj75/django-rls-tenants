"""Django application configuration for django-rls-tenants."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from django.apps import AppConfig

if TYPE_CHECKING:
    from django_rls_tenants.tenants.conf import RLSTenantsConfig

logger = logging.getLogger("django_rls_tenants")


def _apply_request_gucs(db_alias: str, conf: RLSTenantsConfig) -> None:
    """Set the request's GUCs on a new connection, from the tenant ``ContextVar``.

    Used on the thread where the middleware set the GUCs, whose cleanup clears
    them again. Failures are logged, not raised.

    Args:
        db_alias: Alias of the new connection.
        conf: ``RLSTenantsConfig`` instance.
    """
    from django_rls_tenants.rls.guc import clear_guc, set_guc  # noqa: PLC0415
    from django_rls_tenants.tenants.state import get_current_tenant_id  # noqa: PLC0415

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


def _leave_connection_without_context(db_alias: str, conf: RLSTenantsConfig) -> None:
    """Clear both GUCs on a connection opened off the thread that set the request's GUCs.

    The middleware clears GUCs only on its own thread, so values set on this
    connection would stay for the life of the thread and reach later requests
    (#130). Clearing, not just skipping, also drops stale values that a reused
    (pooled) connection may carry. Failures are logged, not raised.

    Args:
        db_alias: Alias of the new connection.
        conf: ``RLSTenantsConfig`` instance.
    """
    from django_rls_tenants.rls.guc import clear_guc  # noqa: PLC0415

    try:
        clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
        clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
    except Exception:
        logger.warning(
            "Failed to clear RLS GUCs on a connection for alias %r opened outside the "
            "request's thread.",
            db_alias,
            exc_info=True,
        )
    else:
        logger.debug(
            "Connection for alias %r opened outside the request's thread; left without "
            "RLS context",
            db_alias,
        )


class DjangoRlsTenantsConfig(AppConfig):
    """AppConfig for django-rls-tenants."""

    name = "django_rls_tenants"
    verbose_name = "Django RLS Tenants"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Connect signal handlers and validate configuration on startup."""
        from django.core.signals import request_finished, setting_changed  # noqa: PLC0415
        from django.db.backends.signals import connection_created  # noqa: PLC0415

        from django_rls_tenants.rls.guc import clear_guc  # noqa: PLC0415
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
        # already run on eagerly-opened connections. Only on the thread
        # where the middleware set the GUCs: its cleanup clears them there.
        def _set_guc_on_new_connection(
            sender: type,  # noqa: ARG001  -- Django signal API
            connection: object,
            **kwargs: object,  # noqa: ARG001  -- Django signal API
        ) -> None:
            from django_rls_tenants.tenants.middleware import (  # noqa: PLC0415
                _were_gucs_set,
                _were_gucs_set_on_this_thread,
            )

            db_alias: str = getattr(connection, "alias", "")
            conf = rls_tenants_config

            if db_alias not in conf.DATABASES:
                return

            if not _were_gucs_set():
                return  # No active request context

            if _were_gucs_set_on_this_thread():
                _apply_request_gucs(db_alias, conf)
            else:
                # Another thread with a copy of the request's context, such as
                # the thread_sensitive=False hop in which Django builds an
                # error page under ASGI.
                _leave_connection_without_context(db_alias, conf)

        connection_created.connect(_set_guc_on_new_connection, weak=False)

        # ``RLSTenantsConfig`` caches ``settings.RLS_TENANTS`` after the first
        # read. Without this hook, ``override_settings(RLS_TENANTS=...)`` and
        # pytest-django's ``settings`` fixture would be silently ignored for
        # the rest of the process (#100). Django sends ``setting_changed`` on
        # both enter and exit of an override, so the cache is rebuilt both ways.
        def _reload_config_on_setting_changed(
            sender: object,  # noqa: ARG001  -- Django signal API
            setting: str,
            **kwargs: object,  # noqa: ARG001  -- Django signal API
        ) -> None:
            if setting == "RLS_TENANTS":
                rls_tenants_config.reload()

        # Tests that override INSTALLED_APPS can call ready() more than once.
        setting_changed.connect(
            _reload_config_on_setting_changed,
            weak=False,
            dispatch_uid="django_rls_tenants.reload_config_on_setting_changed",
        )

        # Import checks module so @register decorators are activated.
        import django_rls_tenants.tenants.checks  # noqa: PLC0415, F401

        # Validate configuration at startup
        from django_rls_tenants.exceptions import RLSConfigurationError  # noqa: PLC0415

        with contextlib.suppress(RLSConfigurationError):
            _ = rls_tenants_config.TENANT_MODEL

        # Auto-detect M2M fields on RLSProtectedModel subclasses and
        # register RLSM2MConstraint on their auto-generated through tables.
        # Must run after all models are loaded (ready() is the right hook).
        from django_rls_tenants.tenants.models import register_m2m_rls  # noqa: PLC0415

        register_m2m_rls()
