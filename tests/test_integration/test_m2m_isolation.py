"""Integration tests for M2M join table RLS coverage.

Verifies that M2M through tables with RLS policies correctly enforce
tenant isolation for both reads and writes.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

from django_rls_tenants.tenants.context import admin_context, tenant_context
from tests.test_app.models import Project, ProtectedUser, SelfRefModel

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# M2M isolation -- both sides RLS-protected (Project.members)
# ---------------------------------------------------------------------------


class TestM2MBothSidesIsolation:
    """Verify isolation on M2M where both sides are RLS-protected."""

    def test_tenant_a_sees_only_own_members(self, sample_projects, tenant_a):
        """Tenant A can only see its own project-member links."""
        with tenant_context(tenant_a.pk):
            proj = Project.objects.get(name="Project A")
            members = list(proj.members.values_list("email", flat=True))
        assert members == ["alice@a.com"]

    def test_tenant_b_sees_only_own_members(self, sample_projects, tenant_b):
        """Tenant B can only see its own project-member links."""
        with tenant_context(tenant_b.pk):
            proj = Project.objects.get(name="Project B")
            members = list(proj.members.values_list("email", flat=True))
        assert members == ["bob@b.com"]

    def test_tenant_a_cannot_see_tenant_b_members(self, sample_projects, tenant_a):
        """Tenant A cannot see Tenant B's member links via the through table."""
        with tenant_context(tenant_a.pk):
            # Project B is invisible, and its through-table links are too
            through_count = Project.members.through.objects.count()
        # Should only see tenant A's links
        assert through_count == 1

    def test_admin_sees_all_members(self, sample_projects):
        """Admin context sees all M2M links."""
        with admin_context():
            total = Project.members.through.objects.count()
        assert total == 2

    def test_no_context_returns_nothing(self, sample_projects):
        """No context = zero M2M links (fail-closed)."""
        count = Project.members.through.objects.count()
        assert count == 0

    def test_raw_sql_m2m_isolation(self, sample_projects, tenant_a):
        """Raw SQL on the through table respects RLS."""
        with tenant_context(tenant_a.pk), connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM test_project_members")
            count = cur.fetchone()[0]
        assert count == 1

    def test_raw_sql_no_context_returns_nothing(self, sample_projects):
        """Raw SQL without tenant context returns zero rows (fail-closed)."""
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM test_project_members")
            count = cur.fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# M2M isolation -- one side RLS-protected (Project.tags)
# ---------------------------------------------------------------------------


class TestM2MOneSideIsolation:
    """Verify isolation on M2M where only one side is RLS-protected."""

    def test_tenant_a_sees_only_own_tags(self, sample_projects, tenant_a):
        """Tenant A can only see tag links for its own projects."""
        with tenant_context(tenant_a.pk):
            proj = Project.objects.get(name="Project A")
            tags = list(proj.tags.values_list("name", flat=True))
        assert tags == ["Tag X"]

    def test_tenant_a_cannot_see_tenant_b_tag_links(self, sample_projects, tenant_a):
        """Tenant A cannot see Tenant B's project-tag links."""
        with tenant_context(tenant_a.pk):
            count = Project.tags.through.objects.count()
        assert count == 1

    def test_admin_sees_all_tag_links(self, sample_projects):
        """Admin sees all project-tag links."""
        with admin_context():
            total = Project.tags.through.objects.count()
        assert total == 2


# ---------------------------------------------------------------------------
# Self-referential M2M
# ---------------------------------------------------------------------------


class TestSelfReferentialM2M:
    """Verify isolation on self-referential M2M."""

    def test_tenant_a_sees_own_friends(self, sample_selfref, tenant_a):
        """Tenant A can see its own friendship links."""
        with tenant_context(tenant_a.pk):
            sr = SelfRefModel.objects.get(name="SR A1")
            friends = list(sr.friends.values_list("name", flat=True))
        assert friends == ["SR A2"]

    def test_tenant_a_cannot_see_tenant_b_friends(self, sample_selfref, tenant_a):
        """Tenant A cannot see Tenant B's friend links."""
        with tenant_context(tenant_a.pk):
            total = SelfRefModel.friends.through.objects.count()
        assert total == 1

    def test_admin_sees_all_friends(self, sample_selfref):
        """Admin sees all friendship links."""
        with admin_context():
            total = SelfRefModel.friends.through.objects.count()
        assert total == 2


# ---------------------------------------------------------------------------
# M2M write operations
# ---------------------------------------------------------------------------


class TestM2MWriteIsolation:
    """Verify WITH CHECK prevents cross-tenant M2M links."""

    def test_add_member_with_matching_context(
        self, sample_projects, sample_protected_users, tenant_a
    ):
        """Adding a member within the same tenant context succeeds."""
        with admin_context():
            extra_user = ProtectedUser.objects.create(email="carol@a.com", tenant=tenant_a)
        with tenant_context(tenant_a.pk):
            proj = Project.objects.get(name="Project A")
            proj.members.add(extra_user)
            members = list(proj.members.values_list("email", flat=True))
        assert "carol@a.com" in members

    @pytest.mark.django_db(transaction=True)
    def test_add_cross_tenant_member_fails(
        self, sample_projects, sample_protected_users, tenant_a
    ):
        """Adding a member from another tenant violates WITH CHECK.

        The subquery-based WITH CHECK clause requires both FK sides
        to belong to the current tenant.
        """
        from django.db.utils import InternalError, ProgrammingError  # noqa: PLC0415

        b_user = sample_protected_users["b"]
        with tenant_context(tenant_a.pk):
            proj = Project.objects.get(name="Project A")
            with pytest.raises((InternalError, ProgrammingError)):
                proj.members.add(b_user)


# ---------------------------------------------------------------------------
# check_rls reports M2M tables
# ---------------------------------------------------------------------------


class TestCheckRlsM2M:
    """Verify check_rls command reports M2M through tables."""

    def test_check_rls_includes_m2m_tables(self):
        """check_rls output includes M2M through table verification."""
        out = StringIO()
        call_command("check_rls", stdout=out)
        output = out.getvalue()
        assert "M2M through tables" in output

    def test_check_rls_succeeds_with_m2m_policies(self):
        """check_rls exits cleanly when M2M policies are applied."""
        out = StringIO()
        call_command("check_rls", stdout=out)
        output = out.getvalue()
        assert "verified" in output.lower()
