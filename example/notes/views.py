from django.contrib.auth.decorators import login_required
from django.http import HttpResponseServerError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django_rls_tenants import clear_tenant_selection, switch_tenant
from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.tenants import get_current_tenant_id, get_rls_context_active

from .models import Category, Note
from .services import note_stats_in_context
from .tasks import count_notes


@login_required
def note_list(request):
    if not get_rls_context_active():
        # A user with memberships but no tenant of their own, who has not
        # selected one yet: RLSTenantMiddleware set no context (fail-closed),
        # and STRICT_MODE would refuse the queries. The header offers the
        # tenant switcher.
        return render(request, "notes/note_list.html", {"notes": [], "categories": []})
    # RLS automatically filters -- only current tenant's notes returned.
    # select_related("category") auto-propagates the tenant filter to
    # the joined Category table, enabling composite index usage on both sides.
    notes = Note.objects.select_related("category").all()
    categories = Category.objects.all()
    return render(
        request,
        "notes/note_list.html",
        {
            "notes": notes,
            "categories": categories,
        },
    )


@login_required
def note_create(request):
    # The tenant the request runs in: the user's own, or the one they
    # selected with the tenant switcher (a MultiTenantUser).
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        # Admin users have no tenant -- they cannot create tenant-scoped notes.
        return redirect("note_list")
    if request.method == "POST":
        category_id = request.POST.get("category") or None
        Note.objects.create(
            title=request.POST["title"],
            content=request.POST.get("content", ""),
            category_id=category_id,
            tenant_id=tenant_id,
        )
        return redirect("note_list")
    categories = Category.objects.all()
    return render(request, "notes/note_form.html", {"categories": categories})


@login_required
def note_delete(request, pk):
    # Demonstrates handling NoTenantContextError gracefully.
    # With STRICT_MODE=True, queries without tenant context raise
    # NoTenantContextError instead of silently returning empty results.
    try:
        note = get_object_or_404(Note, pk=pk)
    except NoTenantContextError:
        return HttpResponseServerError("Tenant context required to access notes.")
    if request.method == "POST":
        note.delete()
    return redirect("note_list")


@login_required
def note_stats(request):
    """Show per-category statistics of the request's tenant."""
    # The request's context, which RLSTenantMiddleware set for the tenant the
    # user selected. get_note_stats(as_user=request.user) would run in the
    # user's own tenant instead: @with_rls_context reads rls_tenant_id.
    stats = note_stats_in_context()
    # Enqueue a Celery task. The request's RLS context (set by
    # RLSTenantMiddleware) is captured into the task headers by @rls_task, so
    # the task body counts the same notes this user sees -- with no
    # tenant_context() call of its own. The demo runs Celery in eager mode, so
    # .delay() executes inline and .get() returns the result immediately.
    task_total = count_notes.delay().get()
    return render(
        request,
        "notes/note_stats.html",
        {"stats": stats, "task_total": task_total},
    )


@login_required
@require_POST
def select_tenant(request):
    """Select the tenant the user's next requests run in (a ``MultiTenantUser``).

    ``switch_tenant()`` checks that it is one of the user's tenants (else a
    403) and stores it in the session; ``RLSTenantMiddleware`` applies it from
    the next request on. An empty choice goes back to the user's own tenant.
    """
    tenant_id = request.POST.get("tenant", "")
    if tenant_id:
        switch_tenant(request, tenant_id)
    else:
        clear_tenant_selection(request)
    return redirect("note_list")
