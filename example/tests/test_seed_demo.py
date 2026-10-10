"""Tests for the demo accounts created by the seed command."""

from http import HTTPStatus
from io import StringIO

import pytest
from django.core.management import call_command

from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db


def test_seeded_staff_can_use_only_the_scoped_notes_admin(client):
    from accounts.models import User

    call_command("seed_demo", stdout=StringIO())
    staff = User.objects.get(username="staff")
    assert staff.tenant.slug == "acme"
    assert staff.is_staff
    assert not staff.is_superuser
    assert not staff.is_tenant_admin
    assert staff.get_all_permissions() == {
        f"notes.{action}_{model}"
        for action in ("add", "change", "delete", "view")
        for model in ("note", "category")
    }
    assert client.login(username="staff", password="staff1234")  # noqa: S106 -- demo login
    response = client.get("/admin/notes/note/")
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Q3 Planning" in body
    assert "Product Launch" not in body
    assert "TPS Reports" not in body
    response = client.get("/admin/notes/category/")
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Hiring" in body
    assert "Investors" not in body
    assert "Operations" not in body
    assert client.get("/admin/accounts/user/").status_code == HTTPStatus.FORBIDDEN
    assert client.get("/admin/tenants/tenant/").status_code == HTTPStatus.FORBIDDEN


def test_seed_backfills_staff_without_resetting_existing_data(client):
    from accounts.models import User
    from notes.models import Note

    call_command("seed_demo", stdout=StringIO())
    # Simulate a volume from before the staff account was introduced.
    User.objects.get(username="staff").delete()
    alice = User.objects.get(username="alice")
    alice.email = "custom@example.com"
    alice.save()
    with rls_bypass():
        note = Note.objects.get(title="Q3 Planning")
        note.title = "Edited roadmap"
        note.save()
        original_notes = set(Note.objects.values_list("pk", "title"))

    call_command("seed_demo", stdout=StringIO())
    assert client.login(username="staff", password="staff1234")  # noqa: S106 -- demo login
    assert client.get("/admin/notes/note/").status_code == HTTPStatus.OK
    staff = User.objects.get(username="staff")
    staff.set_password("custom-staff-password")
    staff.save()

    call_command("seed_demo", stdout=StringIO())
    staff.refresh_from_db()
    assert staff.check_password("custom-staff-password")
    assert User.objects.filter(username="staff").count() == 1
    alice.refresh_from_db()
    assert alice.email == "custom@example.com"
    with rls_bypass():
        assert set(Note.objects.values_list("pk", "title")) == original_notes
