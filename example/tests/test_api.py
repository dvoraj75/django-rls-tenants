"""Tests for the REST API in ``notes/api.py`` (``django_rls_tenants.contrib.drf``).

The clients authenticate with a DRF token, which ``RLSTenantMiddleware`` cannot
see (DRF authenticates inside the view): every context these requests run in is
the one ``RLSTenantAPIViewMixin`` opens. The database role is a non-superuser,
so the isolation asserted here is PostgreSQL's.
"""

from http import HTTPStatus

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db

NOTES = "/api/notes/"


@pytest.fixture
def notes(tenant_acme, tenant_globex):
    """Two Acme notes and one Globex note."""
    from notes.models import Note

    with rls_bypass():
        return {
            "acme": [
                Note.objects.create(title="Acme roadmap", tenant=tenant_acme),
                Note.objects.create(title="Acme todo", tenant=tenant_acme),
            ],
            "globex": [Note.objects.create(title="Globex plan", tenant=tenant_globex)],
        }


def token_client(user):
    """An API client that sends ``user``'s token."""
    token = Token.objects.create(user=user)
    return APIClient(headers={"Authorization": f"Token {token.key}"})


def titles(response):
    return sorted(note["title"] for note in response.json())


class TestNotesApi:
    def test_api_root_answers_token_clients(self, acme_user):
        response = token_client(acme_user).get("/api/")

        assert response.status_code == HTTPStatus.OK
        assert "notes" in response.json()

    def test_tenant_user_lists_only_their_notes(self, acme_user, notes):
        response = token_client(acme_user).get(NOTES)

        assert response.status_code == HTTPStatus.OK
        assert titles(response) == ["Acme roadmap", "Acme todo"]
        assert "tenant" not in response.json()[0]

    def test_admin_lists_every_note(self, admin_user, notes):
        response = token_client(admin_user).get(NOTES)

        assert response.status_code == HTTPStatus.OK
        assert titles(response) == ["Acme roadmap", "Acme todo", "Globex plan"]

    def test_anonymous_request_is_refused(self, notes):
        assert APIClient().get(NOTES).status_code == HTTPStatus.UNAUTHORIZED

    def test_create_is_stamped_with_the_users_tenant(self, acme_user, tenant_acme, tenant_globex):
        from notes.models import Note

        response = token_client(acme_user).post(
            NOTES, {"title": "From the API", "tenant": tenant_globex.pk}, format="json"
        )

        assert response.status_code == HTTPStatus.CREATED
        with rls_bypass():
            assert Note.objects.get(title="From the API").tenant_id == tenant_acme.pk

    def test_other_tenants_note_is_not_found(self, acme_user, notes):
        globex_note = notes["globex"][0]

        response = token_client(acme_user).get(f"{NOTES}{globex_note.pk}/")

        assert response.status_code == HTTPStatus.NOT_FOUND
