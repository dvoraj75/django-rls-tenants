"""Tests for the ``safe_tenant_sql()`` raw aggregate in ``notes/services.py``."""

from http import HTTPStatus

import pytest

from django_rls_tenants.tenants.testing import rls_bypass

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded_notes(tenant_acme, tenant_globex):
    """Notes for two tenants with known content lengths, keyed by tenant."""
    from notes.models import Note

    with rls_bypass():
        return {
            "acme": [
                Note.objects.create(title="a", content="12345", tenant=tenant_acme),
                Note.objects.create(title="b", content="123", tenant=tenant_acme),
            ],
            "globex": [
                Note.objects.create(title="c", content="1234567890", tenant=tenant_globex),
            ],
        }


def test_raw_figures_match_the_orm_for_a_tenant_user(seeded_notes, acme_user):
    from notes.services import get_note_stats

    stats = get_note_stats(as_user=acme_user)
    assert stats["total"] == len(seeded_notes["acme"])
    assert stats["raw_total"] == stats["total"]
    assert stats["avg_content_length"] == 4  # noqa: PLR2004 -- round(avg(5, 3))


def test_raw_figures_match_the_orm_for_an_admin(seeded_notes, admin_user):
    """Admin-bypass parity: the default fragment lets every tenant through."""
    from notes.services import get_note_stats

    stats = get_note_stats(as_user=admin_user)
    assert stats["total"] == sum(map(len, seeded_notes.values()))
    assert stats["raw_total"] == stats["total"]
    assert stats["avg_content_length"] == 6  # noqa: PLR2004 -- round(avg(5, 3, 10))


def test_stats_page_renders_raw_and_task_figures(seeded_notes, acme_user, client):
    expected = len(seeded_notes["acme"])
    client.force_login(acme_user)
    response = client.get("/stats/")
    assert response.status_code == HTTPStatus.OK
    assert response.context["stats"]["raw_total"] == expected
    assert response.context["task_total"] == expected
    assert f"Raw count</strong>: {expected} notes".encode() in response.content
    assert f"Counted by the task</strong>: {expected} notes".encode() in response.content
