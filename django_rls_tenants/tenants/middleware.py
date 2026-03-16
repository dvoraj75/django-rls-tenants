"""Django middleware that sets RLS context per-request.

Provides ``RLSTenantMiddleware`` which reads the authenticated user
and sets GUC variables for the duration of each request.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from django.utils.deprecation import MiddlewareMixin

from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _resolve_user_guc_vars
from django_rls_tenants.tenants.state import set_current_tenant_id

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("django_rls_tenants")

# Thread-local flag indicating whether GUCs were set during this request.
# Used by the ``request_finished`` safety net in ``apps.py`` to skip
# redundant cleanup on requests that never set GUCs (e.g., unauthenticated).
_rls_state = threading.local()


def _mark_gucs_set() -> None:
    """Mark that GUCs were set on the current thread."""
    _rls_state.gucs_set = True


def _clear_gucs_set_flag() -> None:
    """Clear the thread-local GUC flag."""
    _rls_state.gucs_set = False


def _were_gucs_set() -> bool:
    """Check whether GUCs were set on the current thread."""
    return getattr(_rls_state, "gucs_set", False)


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
        """Set GUC variables based on the authenticated user.

        If the first ``set_guc`` succeeds but the second fails (e.g.,
        broken connection), both GUCs are cleared to prevent partial
        state from leaking tenant context.
        """
        if not hasattr(request, "user") or not request.user.is_authenticated:
            return

        conf = rls_tenants_config
        user: Any = request.user

        try:
            guc_vars = _resolve_user_guc_vars(user, conf)
            for guc_name, guc_value in guc_vars.items():
                if guc_value:
                    set_guc(guc_name, guc_value, is_local=conf.USE_LOCAL_SET)
                else:
                    clear_guc(guc_name, is_local=conf.USE_LOCAL_SET)

            # Set auto-scope state for RLSManager.get_queryset()
            tenant_value = guc_vars.get(conf.GUC_CURRENT_TENANT, "")
            if tenant_value:
                set_current_tenant_id(tenant_value)
            else:
                set_current_tenant_id(None)
            _mark_gucs_set()
        except Exception:
            logger.exception("Failed to set RLS GUC variables, clearing both to prevent leak")
            set_current_tenant_id(None)
            try:
                clear_guc(conf.GUC_IS_ADMIN)
                clear_guc(conf.GUC_CURRENT_TENANT)
            except Exception:  # noqa: S110  -- best-effort cleanup, connection may be dead
                pass
            raise

    def process_response(
        self,
        request: HttpRequest,  # noqa: ARG002  -- required by MiddlewareMixin
        response: HttpResponse,
    ) -> HttpResponse:
        """Clear GUC variables and auto-scope state to prevent cross-request leaks."""
        set_current_tenant_id(None)
        conf = rls_tenants_config
        if not conf.USE_LOCAL_SET:
            clear_guc(conf.GUC_IS_ADMIN)
            clear_guc(conf.GUC_CURRENT_TENANT)
        _clear_gucs_set_flag()
        return response
