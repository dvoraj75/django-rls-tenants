"""Django application configuration for django-rls-tenants."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from django.apps import AppConfig

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper

    from django_rls_tenants.tenants.conf import RLSTenantsConfig

logger = logging.getLogger("django_rls_tenants")


def _apply_request_gucs(connection: BaseDatabaseWrapper, conf: RLSTenantsConfig) -> None:
    """Set the active context's GUCs on a new connection, in one statement.

    Used on the thread where the middleware set the GUCs, whose cleanup clears
    them again. Writes to the connection the signal sent, which is not always
    the thread's connection for its alias. Failures are logged, not raised;
    with session GUCs the affinity guard applies the context again before the
    connection's first statement, and raises if that fails too.

    Args:
        connection: The new connection, as sent by ``connection_created``.
        conf: ``RLSTenantsConfig`` instance.
    """
    from django_rls_tenants.tenants.affinity import set_gucs  # noqa: PLC0415
    from django_rls_tenants.tenants.state import _get_context_gucs  # noqa: PLC0415

    context_gucs = _get_context_gucs()
    if context_gucs is None:
        return
    try:
        set_gucs(connection, context_gucs, is_local=conf.USE_LOCAL_SET)
    except Exception:
        logger.warning(
            "Failed to set RLS GUCs on new connection for alias %r. "
            "Queries on this connection may return empty results.",
            connection.alias,
            exc_info=True,
        )


def _clear_new_connection_gucs(connection: BaseDatabaseWrapper, reason: str) -> None:
    """Clear every GUC the RLS policies read on a connection that must start without context.

    Two kinds of connection get this. One opened off the thread that set the
    request's GUCs, which the middleware's cleanup never reaches (#130): the
    affinity guard applies the context to the statements run on it, and takes
    it off again before the first statement run without one (#41). And one
    checked out of a connection pool, which may still carry a previous
    borrower's session GUCs if that borrower's cleanup did not reach it (#96).
    One statement, so one round trip. The guard records the cleared GUCs.

    A failure on a pooled connection fails the checkout: its session is ended,
    so the pool discards it instead of reusing it, and the error is raised to
    the code that asked for the connection. Any other connection is
    a new session, so a failure there is logged, not raised.

    Args:
        connection: The new connection, as sent by ``connection_created``.
        reason: Why the connection is cleared, completing the log messages.
    """
    from django_rls_tenants.pool import _clear_policy_gucs  # noqa: PLC0415
    from django_rls_tenants.tenants.affinity import writing  # noqa: PLC0415

    try:
        with writing(connection, None):
            _clear_policy_gucs(connection)
    except Exception:
        pooled = _is_pooled(connection)
        logger.warning(
            "Failed to clear RLS GUCs on a connection for alias %r %s%s.",
            connection.alias,
            reason,
            "; discarding it" if pooled else "",
            exc_info=True,
        )
        if not pooled:
            return
        # End the session without Connection.close(), which hands the
        # connection back to a pool with close_returns=True. Django's close()
        # then returns it once, and the pool discards a closed connection and
        # replaces it.
        with contextlib.suppress(Exception):
            connection.connection.pgconn.finish()
        with contextlib.suppress(Exception):
            connection.close()
        raise
    logger.debug("Connection for alias %r %s; RLS GUCs cleared", connection.alias, reason)


def _is_pooled(connection: object) -> bool:
    """Whether ``connection`` comes from Django's psycopg pool (``OPTIONS["pool"]``)."""
    from django_rls_tenants.pool import _pool_options  # noqa: PLC0415

    return _pool_options(getattr(connection, "settings_dict", {})) is not None


def _prepare_new_connection(connection: BaseDatabaseWrapper) -> None:
    """Set up a new database connection for RLS.

    Runs on ``connection_created``. It installs the GUC affinity guard, which
    applies the active context's GUCs before each statement on a connection of
    an alias in ``RLS_TENANTS["DATABASES"]`` (#41); nothing else happens on
    other aliases. When the connection opens mid-request on the thread where the
    middleware set the GUCs (a replica opened lazily by a router, say), the
    GUCs are also set right away. On any other thread holding a copy of the
    request's context, the connection is cleared first (#130). Django sends
    the signal on every checkout from a connection pool, and such a connection
    is cleared first as well (#96).

    Args:
        connection: The new connection, as sent by ``connection_created``.
    """
    from django_rls_tenants.tenants.affinity import UNKNOWN, install  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.middleware import (  # noqa: PLC0415
        _were_gucs_set,
        _were_gucs_set_on_this_thread,
    )

    pooled = _is_pooled(connection)
    # A pooled session may hold a previous borrower's GUCs until the clear below.
    # Installed on every connection; the guard only acts on configured aliases,
    # so an alias that joins RLS_TENANTS["DATABASES"] later is covered too.
    install(connection, values=UNKNOWN if pooled else None)

    conf = rls_tenants_config
    if connection.alias not in conf.DATABASES:
        return

    on_request_thread = _were_gucs_set_on_this_thread()
    if _were_gucs_set() and not on_request_thread:
        # Another thread with a copy of the request's context, such as the
        # thread_sensitive=False hop in which Django builds an error page under
        # ASGI. The middleware's cleanup never reaches this connection. It starts
        # clean, bypass flags included; the guard applies the context before each
        # statement in the hop and clears it before the first statement run
        # without one.
        _clear_new_connection_gucs(connection, "opened outside the request's thread")
        return

    if pooled:
        # A pooled connection keeps its session GUCs between borrowers; the
        # previous one's cleanup may not have reached it. With or without a
        # request context, the new borrower starts clean.
        _clear_new_connection_gucs(connection, "checked out of a connection pool")
    if on_request_thread:
        _apply_request_gucs(connection, conf)


class DjangoRlsTenantsConfig(AppConfig):
    """AppConfig for django-rls-tenants."""

    name = "django_rls_tenants"
    verbose_name = "Django RLS Tenants"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Connect signal handlers and validate configuration on startup."""
        from django.core.signals import request_finished, setting_changed  # noqa: PLC0415
        from django.db import connections  # noqa: PLC0415
        from django.db.backends.signals import connection_created  # noqa: PLC0415

        from django_rls_tenants.pool import _declared_policy_guc_names_cached  # noqa: PLC0415
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
                from django_rls_tenants.tenants.affinity import writing  # noqa: PLC0415
                from django_rls_tenants.tenants.state import (  # noqa: PLC0415
                    _set_context_gucs,
                    set_current_tenant_id,
                )

                set_current_tenant_id(None)
                # Otherwise the affinity guard would apply the request's GUCs
                # again before the next statement in this context.
                _set_context_gucs(None)
                conf = rls_tenants_config
                if not conf.USE_LOCAL_SET:
                    for db_alias in conf.DATABASES:
                        # Nothing to clear on a closed connection: its session
                        # is gone, or it is back in a pool, which Django's own
                        # request_finished receiver does first, and the next
                        # checkout clears it. Reopening it here would check out
                        # another pooled connection and keep it.
                        if connections[db_alias].connection is None:
                            continue
                        with writing(db_alias, None):
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

        # Signal handler for new database connections; see _prepare_new_connection().
        def _set_guc_on_new_connection(
            sender: type,  # noqa: ARG001  -- Django signal API
            connection: BaseDatabaseWrapper,
            **kwargs: object,  # noqa: ARG001  -- Django signal API
        ) -> None:
            _prepare_new_connection(connection)

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
            elif setting == "INSTALLED_APPS":
                # The registry changed, as when Django clears its own model caches.
                _declared_policy_guc_names_cached.cache_clear()

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
