"""Tests for a user who belongs to several tenants (``accounts.Membership``).

The consultant belongs to Acme (``tenant``) and Globex (a ``Membership``), so
``User.rls_tenant_ids`` makes them a ``MultiTenantUser``: the switcher in the
page header (``notes.views.select_tenant``, ``switch_tenant()``) and the
``X-Tenant`` header of an API request pick the tenant a request runs in. The
database role is a non-superuser, so the isolation asserted here is PostgreSQL's.
"""

from http import HTTPStatus

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant_initech(db):
    """A tenant the consultant does not belong to."""
    from tenants.models import Tenant

    with rls_bypass():
        return Tenant.objects.create(name="Initech", slug="initech-test")


@pytest.fixture
def notes(tenant_acme, tenant_globex, tenant_initech):
    """One note per tenant."""
    from notes.models import Note

    with rls_bypass():
        return {
            "acme": Note.objects.create(title="Acme roadmap", tenant=tenant_acme),
            "globex": Note.objects.create(title="Globex plan", tenant=tenant_globex),
            "initech": Note.objects.create(title="TPS reports", tenant=tenant_initech),
        }


def _consultant(username, tenant, *member_of):
    from accounts.models import Membership, User

    with rls_bypass():
        user = User.objects.create_user(
            username=username,
            password="testpass",  # noqa: S106 -- demo fixture
            tenant=tenant,
        )
        for member_tenant in member_of:
            Membership.objects.create(user=user, tenant=member_tenant)
    return user


@pytest.fixture
def consultant(tenant_acme, tenant_globex):
    """Works in Acme by default, may switch to Globex."""
    return _consultant("consultant", tenant_acme, tenant_globex)


def titles(response):
    return [note.title for note in response.context["notes"]]


class TestTenantSwitcher:
    def test_default_tenant_and_switcher(self, consultant, notes, client):
        client.force_login(consultant)

        response = client.get("/")

        assert response.status_code == HTTPStatus.OK
        assert titles(response) == ["Acme roadmap"]
        assert response.context["current_tenant"].name == "Acme Corp"
        assert [t.name for t in response.context["selectable_tenants"]] == [
            "Acme Corp",
            "Globex Inc",
        ]

    def test_switching_scopes_the_next_requests(self, consultant, notes, tenant_globex, client):
        client.force_login(consultant)

        switched = client.post("/select-tenant/", {"tenant": tenant_globex.pk})
        listed = client.get("/")
        client.post("/new/", {"title": "Written in Globex"})
        stats = client.get("/stats/")

        assert switched.status_code == HTTPStatus.FOUND
        assert titles(listed) == ["Globex plan"]
        assert listed.context["current_tenant"] == tenant_globex
        with rls_bypass():
            from notes.models import Note

            assert Note.objects.get(title="Written in Globex").tenant_id == tenant_globex.pk
        assert stats.context["stats"]["total"] == 2  # noqa: PLR2004 -- Globex plan + new note
        assert stats.context["task_total"] == 2  # noqa: PLR2004 -- the task sees Globex too

    def test_clearing_returns_to_the_default(self, consultant, notes, tenant_globex, client):
        client.force_login(consultant)
        client.post("/select-tenant/", {"tenant": tenant_globex.pk})

        client.post("/select-tenant/", {"tenant": ""})

        assert titles(client.get("/")) == ["Acme roadmap"]

    def test_tenant_of_another_is_refused(self, consultant, notes, tenant_initech, client):
        client.force_login(consultant)

        response = client.post("/select-tenant/", {"tenant": tenant_initech.pk})

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert titles(client.get("/")) == ["Acme roadmap"]

    def test_single_tenant_user_gets_no_switcher(self, acme_user, notes, client):
        client.force_login(acme_user)

        response = client.get("/")

        assert titles(response) == ["Acme roadmap"]
        assert response.context["selectable_tenants"] == []

    def test_member_without_a_tenant_of_their_own_picks_one(self, tenant_globex, notes, client):
        roaming = _consultant("roaming", None, tenant_globex)
        client.force_login(roaming)

        before = client.get("/")
        client.post("/select-tenant/", {"tenant": tenant_globex.pk})
        after = client.get("/")

        # No context until a tenant is selected (fail-closed), but the page renders.
        assert before.status_code == HTTPStatus.OK
        assert titles(before) == []
        assert before.context["current_tenant"] is None
        assert b"Select a tenant" in before.content
        assert titles(after) == ["Globex plan"]


class TestApiTenantHeader:
    def _client(self, user):
        token = Token.objects.create(user=user)
        return APIClient(headers={"Authorization": f"Token {token.key}"})

    def test_header_selects_a_tenant_of_the_user(self, consultant, notes, tenant_globex):
        client = self._client(consultant)

        selected = client.get("/api/notes/", headers={"X-Tenant": str(tenant_globex.pk)})
        default = client.get("/api/notes/")

        assert selected.status_code == HTTPStatus.OK
        assert [note["title"] for note in selected.json()] == ["Globex plan"]
        assert [note["title"] for note in default.json()] == ["Acme roadmap"]

    def test_header_of_another_tenant_is_a_403(self, consultant, notes, tenant_initech):
        response = self._client(consultant).get(
            "/api/notes/", headers={"X-Tenant": str(tenant_initech.pk)}
        )

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert response.json() == {"detail": "The selected tenant is not one you can access."}
