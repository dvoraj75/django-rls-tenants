"""Tests for the ``RLSTenantModelAdmin``-based example admin (``notes/admin.py``).

Drives the real admin through Django's test client to show what a reviewer sees
at ``/admin/``:

* a tenant-scoped staff user (``is_staff=True``, ``is_tenant_admin=False``, a
  tenant assigned -- the seeded ``staff`` account) gets an auto-scoped changelist,
  tenant-scoped related dropdowns, and a hidden + auto-assigned tenant FK;
* a cross-tenant admin (``is_tenant_admin=True`` -- the seeded ``admin`` account)
  sees every tenant and gets the session-backed tenant switcher.

The scoping asserted here is the ORM-level auto-scoping that
``RLSTenantModelAdmin`` activates for each view; it holds regardless of the
database role the tests connect with.
"""

from http import HTTPStatus

import pytest
from django.contrib.auth.models import Permission

from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db

NOTE_CHANGELIST = "/admin/notes/note/"
NOTE_ADD = "/admin/notes/note/add/"


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def acme_staff(db, tenant_acme):
    """Tenant-scoped staff user, set up exactly like ``seed_demo``'s ``staff``."""
    from accounts.models import User

    with rls_bypass():
        user = User.objects.create_user(
            username="staff",
            email="staff@acme.com",
            password="testpass",  # noqa: S106 -- demo fixture
            tenant=tenant_acme,
            is_tenant_admin=False,
            is_staff=True,
        )
        # Non-superuser staff need model permissions to see the notes app.
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="notes"))
    return user


@pytest.fixture
def cross_tenant_admin(db):
    """Cross-tenant admin, set up like ``seed_demo``'s ``admin``."""
    from accounts.models import User

    with rls_bypass():
        return User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="testpass",  # noqa: S106 -- demo fixture
            tenant=None,
            is_tenant_admin=True,
        )


@pytest.fixture
def notes(db, tenant_acme, tenant_globex):
    """One note per tenant, so scoping is visible in the changelist."""
    from notes.models import Note

    with rls_bypass():
        return {
            "acme": Note.objects.create(title="Acme roadmap", tenant=tenant_acme),
            "globex": Note.objects.create(title="Globex launch", tenant=tenant_globex),
        }


# ── Tenant-scoped staff ────────────────────────────────────────────


class TestScopedStaffAdmin:
    """The ``staff`` account only ever sees -- and writes -- Acme data."""

    def test_changelist_is_scoped_to_own_tenant(self, client, acme_staff, notes):
        client.force_login(acme_staff)
        response = client.get(NOTE_CHANGELIST)
        assert response.status_code == HTTPStatus.OK
        body = response.content.decode()
        assert "Acme roadmap" in body
        assert "Globex launch" not in body

    def test_no_tenant_switcher_rendered(self, client, acme_staff, notes):
        client.force_login(acme_staff)
        body = client.get(NOTE_CHANGELIST).content.decode()
        assert "By tenant" not in body

    def test_add_form_hides_tenant_fk(self, client, acme_staff):
        client.force_login(acme_staff)
        body = client.get(NOTE_ADD).content.decode()
        assert 'name="title"' in body
        assert 'name="tenant"' not in body

    def test_category_dropdown_lists_only_own_tenant(
        self, client, acme_staff, tenant_acme, tenant_globex
    ):
        """Related dropdowns are scoped too: they query ``Category.objects`` in context."""
        from notes.models import Category

        with rls_bypass():
            Category.objects.create(name="Acme Engineering", tenant=tenant_acme)
            Category.objects.create(name="Globex Security", tenant=tenant_globex)

        client.force_login(acme_staff)
        body = client.get(NOTE_ADD).content.decode()
        assert "Acme Engineering" in body
        assert "Globex Security" not in body

    def test_add_auto_assigns_tenant(self, client, acme_staff, tenant_acme):
        from notes.models import Note

        client.force_login(acme_staff)
        response = client.post(
            NOTE_ADD,
            {"title": "Added in admin", "content": "", "_save": "Save"},
        )
        assert response.status_code == HTTPStatus.FOUND
        with rls_bypass():
            assert Note.objects.get(title="Added in admin").tenant_id == tenant_acme.pk

    def test_cannot_open_other_tenant_note(self, client, acme_staff, notes):
        client.force_login(acme_staff)
        response = client.get(f"{NOTE_CHANGELIST}{notes['globex'].pk}/change/")
        # Invisible under Acme -> the admin redirects away instead of showing it.
        assert response.status_code == HTTPStatus.FOUND

    def test_can_open_own_note(self, client, acme_staff, notes):
        client.force_login(acme_staff)
        response = client.get(f"{NOTE_CHANGELIST}{notes['acme'].pk}/change/")
        assert response.status_code == HTTPStatus.OK
        assert "Acme roadmap" in response.content.decode()


# ── Cross-tenant admin ─────────────────────────────────────────────


class TestCrossTenantAdmin:
    """The ``admin`` account sees everything and can switch tenants."""

    def test_sees_all_tenants(self, client, cross_tenant_admin, notes):
        client.force_login(cross_tenant_admin)
        body = client.get(NOTE_CHANGELIST).content.decode()
        assert "Acme roadmap" in body
        assert "Globex launch" in body

    def test_tenant_switcher_is_rendered(self, client, cross_tenant_admin, notes):
        client.force_login(cross_tenant_admin)
        body = client.get(NOTE_CHANGELIST).content.decode()
        assert "By tenant" in body
        assert "Acme Corp" in body
        assert "Globex Inc" in body

    def test_add_form_shows_tenant_fk_without_selection(self, client, cross_tenant_admin):
        client.force_login(cross_tenant_admin)
        body = client.get(NOTE_ADD).content.decode()
        assert 'name="tenant"' in body

    def test_switcher_scopes_changelist_and_auto_assigns(
        self, client, cross_tenant_admin, notes, tenant_acme
    ):
        from notes.models import Note

        client.force_login(cross_tenant_admin)
        # Pick Acme in the switcher: the changelist is scoped...
        body = client.get(NOTE_CHANGELIST, {"rls_tenant": str(tenant_acme.pk)}).content.decode()
        assert "Acme roadmap" in body
        assert "Globex launch" not in body
        # ...the selection persists in the session, the FK is hidden on add...
        assert 'name="tenant"' not in client.get(NOTE_ADD).content.decode()
        # ...and a new note is stamped with Acme without picking it by hand.
        response = client.post(
            NOTE_ADD,
            {"title": "Switched", "content": "", "_save": "Save"},
        )
        assert response.status_code == HTTPStatus.FOUND
        with rls_bypass():
            assert Note.objects.get(title="Switched").tenant_id == tenant_acme.pk

    def test_all_clears_selection(self, client, cross_tenant_admin, notes, tenant_acme):
        client.force_login(cross_tenant_admin)
        client.get(NOTE_CHANGELIST, {"rls_tenant": str(tenant_acme.pk)})
        body = client.get(NOTE_CHANGELIST, {"rls_tenant": "__all__"}).content.decode()
        assert "Acme roadmap" in body
        assert "Globex launch" in body
