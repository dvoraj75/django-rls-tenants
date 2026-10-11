"""Tests for django_rls_tenants.tenants.errors.

The hint constants are user-facing remediation text that several error paths
share (context managers, strict mode, the admin, Celery). Elsewhere they are
only checked loosely (substring or identity), so editing a remediation phrase
would fail nothing. These tests pin the exact wording and, separately, the
API names each hint must keep naming so a reworded hint still points users at
the right fix.
"""

from __future__ import annotations

import pytest

from django_rls_tenants.tenants.errors import (
    HINT_NO_CONTEXT,
    HINT_TENANT_ID_NONE,
    HINT_USER_NO_TENANT,
)


class TestExactWording:
    """Each hint is pinned verbatim."""

    def test_hint_no_context(self):
        assert HINT_NO_CONTEXT == (
            "Establish an RLS context before the query: wrap it in "
            "tenant_context(tenant_id) or admin_context(), scope the queryset with "
            ".for_user(user), or enable RLSTenantMiddleware so each request sets the "
            "context automatically."
        )

    def test_hint_user_no_tenant(self):
        assert HINT_USER_NO_TENANT == (
            "Assign the user to a tenant (set rls_tenant_id) or mark them as a "
            "cross-tenant admin (is_tenant_admin=True). A non-admin TenantUser must "
            "belong to exactly one tenant."
        )

    def test_hint_tenant_id_none(self):
        assert HINT_TENANT_ID_NONE == (
            "Pass a concrete tenant primary key to tenant_context(tenant_id). For "
            "cross-tenant admin access that bypasses tenant scoping, use "
            "admin_context() instead."
        )


class TestRemediationTokens:
    """Each hint keeps naming the API a user must reach for."""

    @pytest.mark.parametrize(
        "token",
        ["tenant_context(tenant_id)", "admin_context()", ".for_user(user)", "RLSTenantMiddleware"],
    )
    def test_no_context_names_every_entry_point(self, token):
        assert token in HINT_NO_CONTEXT

    @pytest.mark.parametrize("token", ["rls_tenant_id", "is_tenant_admin=True"])
    def test_user_no_tenant_names_the_tenant_user_attributes(self, token):
        assert token in HINT_USER_NO_TENANT

    @pytest.mark.parametrize("token", ["tenant_context(tenant_id)", "admin_context() instead"])
    def test_tenant_id_none_points_at_admin_context(self, token):
        assert token in HINT_TENANT_ID_NONE

    def test_hints_are_distinct(self):
        assert len({HINT_NO_CONTEXT, HINT_USER_NO_TENANT, HINT_TENANT_ID_NONE}) == 3
