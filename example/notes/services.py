"""Business logic with automatic RLS context via decorator.

Demonstrates ``@with_rls_context``, which reads the ``as_user`` argument
and wraps the call in ``tenant_context()`` or ``admin_context()``
automatically, and ``safe_tenant_sql()``, which scopes a raw SQL statement
with the same predicate the RLS policy uses.
"""

from django.db import connection
from django.db.models import Count
from django_rls_tenants import safe_tenant_sql, with_rls_context

from .models import Category, Note


def get_raw_note_stats():
    """Return ``(total, avg_content_length)`` for the current tenant via raw SQL.

    Row-Level Security already filters ``notes_note`` for the connection's
    role, so a bare ``SELECT`` would return the right rows. The statement
    still spells the tenant predicate out with ``safe_tenant_sql()``: the
    intent is explicit in code review, and the fragment is built from the
    exact expression the policy uses, so it stays consistent with what the
    database enforces -- including the admin bypass. Inside
    ``admin_context()`` it matches every tenant, giving the same figures the
    ORM reports (see ``tests/test_services.py``).

    The default fragment contains an ``OR <admin>`` branch that the planner
    cannot drive an index with. For a hot tenant-only query use
    ``safe_tenant_sql(include_admin=False)`` -- a bare ``tenant_id = ...``
    match -- as described in the Performance & Indexing guide.

    Must be called inside an RLS context (``get_note_stats`` provides one).
    """
    sql = (
        "SELECT count(*), coalesce(round(avg(length(content))), 0)::int "
        f"FROM notes_note WHERE {safe_tenant_sql()}"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
        total, avg_content_length = cursor.fetchone()
    return total, avg_content_length


@with_rls_context
def get_note_stats(as_user):
    """Return per-category note counts for the current tenant.

    The ``@with_rls_context`` decorator reads ``as_user`` and sets the
    appropriate RLS context before the function body runs. No manual
    ``tenant_context()`` or ``admin_context()`` call needed.

    ``raw_total`` and ``avg_content_length`` come from the raw SQL statement
    in :func:`get_raw_note_stats`; ``raw_total`` always equals ``total``.
    """
    total = Note.objects.count()
    by_category = (
        Category.objects.annotate(note_count=Count("notes"))
        .filter(note_count__gt=0)
        .order_by("-note_count")
        .values_list("name", "note_count")
    )
    uncategorized = Note.objects.filter(category__isnull=True).count()
    raw_total, avg_content_length = get_raw_note_stats()
    return {
        "total": total,
        "by_category": list(by_category),
        "uncategorized": uncategorized,
        "raw_total": raw_total,
        "avg_content_length": avg_content_length,
    }
