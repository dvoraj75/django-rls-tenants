from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .models import Note


@login_required
def note_list(request):
    # RLS automatically filters — only current tenant's notes returned
    notes = Note.objects.all()
    return render(request, "notes/note_list.html", {"notes": notes})


@login_required
def note_create(request):
    if request.method == "POST":
        Note.objects.create(
            title=request.POST["title"],
            content=request.POST.get("content", ""),
            tenant=request.user.tenant,
        )
        return redirect("note_list")
    return render(request, "notes/note_form.html")


@login_required
def note_delete(request, pk):
    note = get_object_or_404(Note, pk=pk)
    if request.method == "POST":
        note.delete()
    return redirect("note_list")
