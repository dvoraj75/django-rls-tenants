"""Tests for ``note_report`` run through ``run_as_tenant`` and ``run_for_all_tenants``.

``note_report`` sets no context of its own; the library's commands supply it.
The database role is a non-superuser, so the counts are RLS-enforced.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded_notes(tenant_acme, tenant_globex):
    """Two Acme notes (one in "Work") and one Globex note."""
    from notes.models import Category, Note

    with rls_bypass():
        work = Category.objects.create(name="Work", tenant=tenant_acme)
        Note.objects.create(title="Acme roadmap", category=work, tenant=tenant_acme)
        Note.objects.create(title="Acme todo", tenant=tenant_acme)
        Note.objects.create(title="Globex plan", tenant=tenant_globex)


def test_run_as_tenant_by_slug(seeded_notes):
    out = StringIO()
    call_command(
        "run_as_tenant", "--tenant", "acme-test", "--lookup", "slug", "note_report", stdout=out
    )
    assert out.getvalue() == "Notes: 2\n  Work: 1\n"


def test_run_as_tenant_admin(seeded_notes):
    out = StringIO()
    call_command("run_as_tenant", "--admin", "note_report", stdout=out)
    assert out.getvalue().startswith("Notes: 3\n")


def test_run_for_all_tenants_json(seeded_notes, tenant_acme, tenant_globex):
    out = StringIO()
    call_command("run_for_all_tenants", "--json", "note_report", stdout=out)
    records = {record["label"]: record for record in map(json.loads, out.getvalue().splitlines())}
    assert records["Acme Corp"]["status"] == "ok"
    assert records["Acme Corp"]["stdout"] == "Notes: 2\n  Work: 1\n"
    assert records["Globex Inc"]["stdout"] == "Notes: 1\n"


def test_without_a_context_strict_mode_refuses(seeded_notes):
    with pytest.raises(NoTenantContextError):
        call_command("note_report", stdout=StringIO())
