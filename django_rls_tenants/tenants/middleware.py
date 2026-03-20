"""Django middleware that sets RLS context per-request.

Provides ``RLSTenantMiddleware`` which reads the authenticated user
and sets GUC variables for the duration of each request.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from django.utils.deprecation import MiddlewareMixin

from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _resolve_user_guc_vars
from django_rls_tenants.tenants.state import (
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("django_rls_tenants")

# Flag indicating whether GUCs were set during this request.
# Uses ``ContextVar`` for proper isolation in both WSGI (threaded)
# and ASGI (async task) deployments. Previous versions used
# ``threading.local`` which shared state across coroutines in ASGI.
_gucs_set_var: ContextVar[bool] = ContextVar("rls_gucs_set", default=False)


def _mark_gucs_set() -> None:
    """Mark that GUCs were set in the current context."""
    _gucs_set_var.set(True)


def _clear_gucs_set_flag() -> None:
    """Clear the GUC flag in the current context."""
    _gucs_set_var.set(False)


def _were_gucs_set() -> bool:
    """Check whether GUCs were set in the current context."""
    return _gucs_set_var.get()


def _clear_gucs_on_all_databases(conf: Any) -> None:
    """Best-effort clear of GUCs on all configured database aliases.

    Used during error handling to prevent partial GUC state from
    leaking. Silently ignores failures on individual aliases.

    Args:
        conf: ``RLSTenantsConfig`` instance.
    """
    for db_alias in conf.DATABASES:
        try:
            clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
            clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
        except Exception:  # noqa: S110  -- best-effort cleanup, connection may be dead
            pass


class RLSTenantMiddleware(MiddlewareMixin):
    """Set RLS context for each authenticated request.

    For authenticated users: sets ``tenant_context`` or ``admin_context``
    based on the user's ``TenantUser`` protocol implementation.

    For unauthenticated requests: no context is set. RLS policies block
    all access to protected tables (fail-closed).

    This is API-agnostic -- works identically for REST, GraphQL, Django
    views, or any other request handler.

    Add to ``MIDDLEWARE``::

        MIDDLEWARE = [
            ...
            "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
        ]
    """

    def process_request(self, request: HttpRequest) -> None:
        """Set GUC variables on all configured databases.

        Iterates over ``RLS_TENANTS["DATABASES"]`` and sets GUCs on each
        alias. If setting GUCs on one database fails, clears all GUCs on
        databases that were already set, then re-raises.
        """
        if not hasattr(request, "user") or not request.user.is_authenticated:
            return

        conf = rls_tenants_config
        user: Any = request.user

        # Track tokens so the except branch can reset via token (not
        # push a new value) when the ContextVar was already set before
        # the failure.
        tenant_token: Token[int | str | None] | None = None
        active_token: Token[bool] | None = None
        try:
            guc_vars = _resolve_user_guc_vars(user, conf)
            self._set_gucs_on_all_databases(guc_vars, conf)

            # Set auto-scope state for RLSManager.get_queryset().
            # Use the original tenant ID from the user (preserving type)
            # rather than the string GUC representation, so that
            # get_current_tenant_id() returns a consistent type.
            if not user.is_tenant_admin:
                tenant_token = set_current_tenant_id(user.rls_tenant_id)
            else:
                tenant_token = set_current_tenant_id(None)
            setattr(request, "_rls_tenant_token", tenant_token)  # noqa: B010  -- dynamic attr on HttpRequest

            # Mark RLS context as active for strict mode. This
            # distinguishes "middleware set context" from "no context".
            active_token = set_rls_context_active(True)
            setattr(request, "_rls_context_active_token", active_token)  # noqa: B010  -- dynamic attr on HttpRequest
            _mark_gucs_set()
        except Exception:
            logger.exception("Failed to set RLS GUC variables, clearing to prevent leak")
            # Reset via token when available to avoid stacking unreset
            # ContextVar values; fall back to set() when the token was
            # never created (error occurred before set call).
            if isinstance(tenant_token, Token):
                reset_current_tenant_id(tenant_token)
            else:
                set_current_tenant_id(None)
            if isinstance(active_token, Token):
                reset_rls_context_active(active_token)
            else:
                set_rls_context_active(False)
            _clear_gucs_on_all_databases(conf)
            raise

    def process_response(
        self,
        request: HttpRequest,
        response: HttpResponse,
    ) -> HttpResponse:
        """Clear GUC variables and auto-scope state to prevent cross-request leaks."""
        self._cleanup_rls_state(request)
        return response

    def process_exception(
        self,
        request: HttpRequest,
        exception: Exception,  # noqa: ARG002  -- required by MiddlewareMixin
    ) -> None:
        """Clear RLS state on unhandled exceptions to prevent ContextVar leaks.

        Without this, a view exception that prevents ``process_response``
        from running would leave the ContextVar set for the remainder of
        the thread (WSGI) or async task (ASGI).
        """
        self._cleanup_rls_state(request)

    @staticmethod
    def _set_gucs_on_all_databases(guc_vars: dict[str, str], conf: Any) -> None:
        """Set GUC variables on all configured database aliases.

        If setting GUCs on one database fails, clears all GUCs on
        databases that were already set, then re-raises.

        Args:
            guc_vars: Mapping of GUC names to values.
            conf: ``RLSTenantsConfig`` instance.
        """
        completed_aliases: list[str] = []
        try:
            for db_alias in conf.DATABASES:
                for guc_name, guc_value in guc_vars.items():
                    if guc_value:
                        set_guc(
                            guc_name,
                            guc_value,
                            is_local=conf.USE_LOCAL_SET,
                            using=db_alias,
                        )
                    else:
                        clear_guc(guc_name, is_local=conf.USE_LOCAL_SET, using=db_alias)
                completed_aliases.append(db_alias)
        except Exception:
            # Rollback GUCs on databases that were already set.
            for done_alias in completed_aliases:
                try:
                    clear_guc(conf.GUC_IS_ADMIN, using=done_alias)
                    clear_guc(conf.GUC_CURRENT_TENANT, using=done_alias)
                except Exception:  # noqa: S110  -- best-effort cleanup
                    pass
            raise

    @staticmethod
    def _cleanup_rls_state(request: HttpRequest) -> None:
        """Reset ContextVars (via tokens if available) and clear GUCs.

        Clears GUCs on all configured database aliases. Skips database
        round-trips for requests where GUCs were never set (e.g.,
        unauthenticated requests, health checks).

        Args:
            request: The Django HTTP request being finalised.
        """
        token = getattr(request, "_rls_tenant_token", None)
        if isinstance(token, Token):
            reset_current_tenant_id(token)
        else:
            # Fallback: no token stored (unauthenticated request or error
            # during process_request before token was saved).
            set_current_tenant_id(None)

        active_token = getattr(request, "_rls_context_active_token", None)
        if isinstance(active_token, Token):
            reset_rls_context_active(active_token)
        else:
            set_rls_context_active(False)

        if not _were_gucs_set():
            return
        conf = rls_tenants_config
        if not conf.USE_LOCAL_SET:
            for db_alias in conf.DATABASES:
                clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
                clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
        _clear_gucs_set_flag()
