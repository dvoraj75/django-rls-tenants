"""Print the current tenant's note counts per category.

The command sets no RLS context of its own: run it through the library's
``run_as_tenant`` or ``run_for_all_tenants`` commands, which wrap it in
``tenant_context()``. Run on its own, ``STRICT_MODE`` (on in this demo)
stops it with ``NoTenantContextError`` instead of reporting zero notes::

    python manage.py run_as_tenant --tenant acme --lookup slug note_report
    python manage.py run_for_all_tenants --json note_report
"""

from django.core.management.base import BaseCommand
from django.db.models import Count

from notes.models import Category, Note


class Command(BaseCommand):
    help = "Print the current tenant's note counts per category (run it via run_as_tenant)."

    def handle(self, *args, **options):
        self.stdout.write(f"Notes: {Note.objects.count()}")
        categories = (
            Category.objects.annotate(note_count=Count("notes"))
            .filter(note_count__gt=0)
            .order_by("-note_count", "name")
        )
        for category in categories:
            self.stdout.write(f"  {category.name}: {category.note_count}")
