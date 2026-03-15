from django.contrib import admin

from notes.models import Note


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "created_at")
    list_filter = ("tenant",)
    search_fields = ("title", "content")
