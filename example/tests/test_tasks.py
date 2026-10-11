"""Tests for the ``@rls_task`` Celery tasks in ``notes/tasks.py``.

Celery runs in eager mode here (``CELERY_TASK_ALWAYS_EAGER`` in
``demo/settings.py``), so ``.delay()`` executes the task inline with no broker
or worker. We inspect the captured headers and replay them after the caller's
context closes, so eager execution cannot mask broken propagation by inheriting
the caller's context. The database role is a non-superuser, so the queries are
RLS-enforced.
"""

import pytest
from celery import Task

from django_rls_tenants import admin_context, tenant_context
from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db


@pytest.fixture
def captured_headers(monkeypatch):
    """Record headers at Celery's eager execution boundary without replacing it."""
    headers = []
    original_apply = Task.apply

    def record_headers(self, *args, **kwargs):
        headers.append(dict(kwargs.get("headers") or {}))
        return original_apply(self, *args, **kwargs)

    monkeypatch.setattr(Task, "apply", record_headers)
    return headers


@pytest.fixture
def seeded_notes(tenant_acme, tenant_globex):
    """Two Acme notes and one Globex note, created outside any tenant scope.

    Returns the notes keyed by tenant so tests can derive the expected counts.
    """
    from notes.models import Category, Note

    with rls_bypass():
        work = Category.objects.create(name="Work", tenant=tenant_acme)
        return {
            "acme": [
                Note.objects.create(title="Acme roadmap", category=work, tenant=tenant_acme),
                Note.objects.create(title="Acme todo", tenant=tenant_acme),
            ],
            "globex": [Note.objects.create(title="Globex plan", tenant=tenant_globex)],
        }


class TestCountNotes:
    """``count_notes`` sees exactly what the enqueuing context sees."""

    def test_counts_only_the_enqueuing_tenant(
        self, seeded_notes, tenant_acme, tenant_globex, captured_headers
    ):
        from notes.tasks import count_notes

        for tenant, notes in (
            (tenant_acme, seeded_notes["acme"]),
            (tenant_globex, seeded_notes["globex"]),
        ):
            with tenant_context(tenant.pk):
                assert count_notes.delay().get() == len(notes)
            headers = captured_headers[-1]
            assert headers == {"rls_tenant_id": tenant.pk, "rls_admin": False}
            assert get_rls_context_active() is False
            assert count_notes.apply(headers=headers).get() == len(notes)
            assert get_current_tenant_id() is None
            assert get_rls_context_active() is False

    def test_counts_every_tenant_under_admin_context(self, seeded_notes, captured_headers):
        from notes.tasks import count_notes

        expected = sum(map(len, seeded_notes.values()))
        with admin_context():
            assert count_notes.delay().get() == expected
        headers = captured_headers[-1]
        assert headers == {"rls_tenant_id": None, "rls_admin": True}
        assert get_rls_context_active() is False
        assert count_notes.apply(headers=headers).get() == expected
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    def test_without_context_strict_mode_raises(self, seeded_notes):
        """STRICT_MODE (on in this demo) makes the unscoped ORM query raise."""
        from notes.tasks import count_notes

        with pytest.raises(NoTenantContextError):
            count_notes.delay().get()

    def test_without_context_and_without_strict_mode_is_fail_closed(self, seeded_notes, settings):
        """Without STRICT_MODE the task runs unscoped and RLS returns zero rows."""
        from notes.tasks import count_notes

        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "STRICT_MODE": False}
        assert count_notes.delay().get() == 0


class TestExportNotes:
    """``export_notes`` is a strict task: no context means no run at all."""

    def test_exports_only_the_enqueuing_tenant(self, seeded_notes, tenant_acme, captured_headers):
        from notes.tasks import export_notes

        expected = [
            {"title": "Acme roadmap", "category": "Work"},
            {"title": "Acme todo", "category": None},
        ]
        with tenant_context(tenant_acme.pk):
            assert export_notes.delay().get() == expected
        headers = captured_headers[-1]
        assert headers == {"rls_tenant_id": tenant_acme.pk, "rls_admin": False}
        assert get_rls_context_active() is False
        assert export_notes.apply(headers=headers).get() == expected
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    def test_without_context_fails_before_the_body_runs(self, seeded_notes, settings):
        """Even with STRICT_MODE off, rls_require_context refuses to run tenant-blind."""
        from notes.tasks import export_notes

        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "STRICT_MODE": False}
        with pytest.raises(NoTenantContextError, match="ran without an RLS context"):
            export_notes.delay()
