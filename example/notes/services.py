"""Business logic with automatic RLS context via decorator.

Demonstrates ``@with_rls_context``, which reads the ``as_user`` argument
and wraps the call in ``tenant_context()`` or ``admin_context()``
automatically.
"""

from django.db.models import Count
from django_rls_tenants import with_rls_context

from .models import Category, Note


@with_rls_context
def get_note_stats(as_user):
    """Return per-category note counts for the current tenant.

    The ``@with_rls_context`` decorator reads ``as_user`` and sets the
    appropriate RLS context before the function body runs. No manual
    ``tenant_context()`` or ``admin_context()`` call needed.
    """
    total = Note.objects.count()
    by_category = (
        Category.objects.annotate(note_count=Count("notes"))
        .filter(note_count__gt=0)
        .order_by("-note_count")
        .values_list("name", "note_count")
    )
    uncategorized = Note.objects.filter(category__isnull=True).count()
    return {
        "total": total,
        "by_category": list(by_category),
        "uncategorized": uncategorized,
    }
