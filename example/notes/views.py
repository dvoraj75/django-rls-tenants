from django.contrib.auth.decorators import login_required
from django.http import HttpResponseServerError
from django.shortcuts import get_object_or_404, redirect, render

from django_rls_tenants import NoTenantContextError

from .models import Category, Note
from .services import get_note_stats


@login_required
def note_list(request):
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
    if not request.user.tenant:
        # Admin users have no tenant -- they cannot create tenant-scoped notes.
        return redirect("note_list")
    if request.method == "POST":
        category_id = request.POST.get("category") or None
        Note.objects.create(
            title=request.POST["title"],
            content=request.POST.get("content", ""),
            category_id=category_id,
            tenant=request.user.tenant,
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
    """Show per-category statistics using @with_rls_context service."""
    stats = get_note_stats(as_user=request.user)
    return render(request, "notes/note_stats.html", {"stats": stats})
