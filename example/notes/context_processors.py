"""Template context for the tenant badge and the tenant switcher in ``base.html``."""

from django_rls_tenants.tenants import get_current_tenant_id


def tenant_switcher(request):
    """Return the tenant the request runs in and, for a user in several, the tenants to pick.

    The ``Tenant`` table is global (not RLS-protected), so it is read whatever
    the request's context. Runs while the template renders, inside the
    context ``RLSTenantMiddleware`` set for the request.
    """
    from tenants.models import Tenant

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.is_tenant_admin:
        return {}
    tenant_id = get_current_tenant_id()
    selectable = set(user.rls_tenant_ids)
    if user.rls_tenant_id is not None:
        selectable.add(user.rls_tenant_id)
    return {
        "current_tenant": Tenant.objects.filter(pk=tenant_id).first() if tenant_id else None,
        # The switcher is offered only when there is another tenant to pick.
        "selectable_tenants": (
            Tenant.objects.filter(pk__in=selectable).order_by("name")
            if len(selectable) > 1 or tenant_id is None
            else []
        ),
    }
