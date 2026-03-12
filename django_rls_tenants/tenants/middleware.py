"""Django middleware that sets RLS context per-request.

Provides ``RLSTenantMiddleware`` which reads the authenticated user
and sets GUC variables for the duration of each request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils.deprecation import MiddlewareMixin

from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


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
        """Set GUC variables based on the authenticated user."""
        if not hasattr(request, "user") or not request.user.is_authenticated:
            return

        conf = rls_tenants_config
        user: Any = request.user

        if user.is_tenant_admin:
            set_guc(
                conf.GUC_IS_ADMIN,
                "true",
                is_local=conf.USE_LOCAL_SET,
            )
            set_guc(
                conf.GUC_CURRENT_TENANT,
                "-1",
                is_local=conf.USE_LOCAL_SET,
            )
        else:
            set_guc(
                conf.GUC_IS_ADMIN,
                "false",
                is_local=conf.USE_LOCAL_SET,
            )
            set_guc(
                conf.GUC_CURRENT_TENANT,
                str(user.rls_tenant_id),
                is_local=conf.USE_LOCAL_SET,
            )

    def process_response(
        self,
        request: HttpRequest,  # noqa: ARG002  -- required by MiddlewareMixin
        response: HttpResponse,
    ) -> HttpResponse:
        """Clear GUC variables to prevent cross-request leaks."""
        conf = rls_tenants_config
        if not conf.USE_LOCAL_SET:
            clear_guc(conf.GUC_IS_ADMIN)
            clear_guc(conf.GUC_CURRENT_TENANT)
        return response
