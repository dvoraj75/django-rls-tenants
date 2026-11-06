"""Tests for the Ninja API in ``notes/ninja_api.py`` (``django_rls_tenants.contrib.ninja``).

The clients authenticate with a DRF token, which ``RLSTenantMiddleware`` cannot
see (Ninja authenticates inside the operation): every context these requests
run in is the one ``RLSTenantNinjaAPI`` opens. The database role is a
non-superuser, so the isolation asserted here is PostgreSQL's.
"""

import json
from http import HTTPStatus

import pytest
from django.test import Client
from rest_framework.authtoken.models import Token

from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db

NOTES = "/ninja/notes/"


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
    """A client that sends ``user``'s token."""
    token = Token.objects.create(user=user)
    return Client(headers={"Authorization": f"Bearer {token.key}"})


def titles(response):
    return sorted(note["title"] for note in response.json())


def post_category(client, category_id):
    return client.post(
        NOTES, {"title": "x", "category_id": category_id}, content_type="application/json"
    )


def normalized(response, category_id):
    return json.loads(response.content.decode().replace(str(category_id), "<id>"))


class TestNinjaNotesApi:
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
        assert Client().get(NOTES).status_code == HTTPStatus.UNAUTHORIZED

    def test_create_is_stamped_with_the_users_tenant(self, acme_user, tenant_acme, tenant_globex):
        from notes.models import Note

        response = token_client(acme_user).post(
            NOTES,
            {"title": "From Ninja", "tenant": tenant_globex.pk},
            content_type="application/json",
        )

        assert response.status_code == HTTPStatus.CREATED
        with rls_bypass():
            assert Note.objects.get(title="From Ninja").tenant_id == tenant_acme.pk

    def test_async_create_is_stamped_with_the_users_tenant(self, acme_user, tenant_acme):
        from notes.models import Note

        response = token_client(acme_user).post(
            f"{NOTES}async/", {"title": "Async"}, content_type="application/json"
        )

        assert response.status_code == HTTPStatus.CREATED
        with rls_bypass():
            assert Note.objects.get(title="Async").tenant_id == tenant_acme.pk

    def test_other_tenants_category_is_rejected_like_a_missing_one(self, acme_user, tenant_globex):
        from notes.models import Category

        with rls_bypass():
            foreign = Category.objects.create(name="Globex only", tenant=tenant_globex)
        client = token_client(acme_user)

        rejected = post_category(client, foreign.pk)
        missing = post_category(client, foreign.pk + 1000)

        assert rejected.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert missing.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        # The same error, except for the id each client sent.
        assert normalized(rejected, foreign.pk) == normalized(missing, foreign.pk + 1000)
