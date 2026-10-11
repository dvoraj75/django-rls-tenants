"""Celery tasks that inherit the caller's RLS context via ``@rls_task``.

Neither task body opens a ``tenant_context()`` or ``admin_context()`` of its
own. ``@rls_task`` captures the context that is active where ``.delay()`` is
called (a request handled by ``RLSTenantMiddleware``, an explicit
``tenant_context()``, ``admin_context()``, ...) into the task headers and
restores it around the body before it runs. The body then simply uses the ORM
and sees exactly what the caller would see -- see ``tests/test_tasks.py`` for
the round-trip under a non-superuser database role.
"""

from django_rls_tenants.contrib.celery import RLSTask, rls_task

from .models import Note


@rls_task
def count_notes():
    """Count the notes visible to the context the task was enqueued from.

    Enqueued from a tenant user's request this is that tenant's count; from
    ``admin_context()`` it is the count across all tenants. Enqueued with no
    context at all it runs unscoped: with ``STRICT_MODE`` (as in this demo) the
    query raises ``NoTenantContextError``; without it RLS simply returns zero
    rows (fail-closed).
    """
    return Note.objects.count()


class StrictRLSTask(RLSTask):
    """Task base that refuses to run without a propagated RLS context.

    Use it for jobs that must never run tenant-blind. A silent empty result --
    an export with no rows, a report of zero -- is worse than an error, so the
    task fails *before* its body runs instead of relying on fail-closed RLS.
    """

    rls_require_context = True


@rls_task(base=StrictRLSTask)
def export_notes():
    """Return the visible notes as plain dicts (title + category name).

    Enqueued without an RLS context this raises ``NoTenantContextError`` right
    away -- the body is never entered -- because the base class sets
    ``rls_require_context = True``.
    """
    return [
        {"title": note.title, "category": note.category.name if note.category else None}
        for note in Note.objects.select_related("category").order_by("title")
    ]
