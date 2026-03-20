"""Tests demonstrating django-rls-tenants testing utilities.

Shows how to use ``rls_bypass``, ``rls_as_tenant``, ``assert_rls_enabled``,
``assert_rls_policy_exists``, and ``assert_rls_blocks_without_context``
in a real project's test suite.

Also demonstrates strict mode: when ``STRICT_MODE=True``, querying
RLS-protected models without an active tenant context raises
``NoTenantContextError`` instead of silently returning empty results.
"""

import pytest

from django_rls_tenants import NoTenantContextError
from django_rls_tenants.tenants.testing import (
    assert_rls_blocks_without_context,
    assert_rls_enabled,
    assert_rls_policy_exists,
    rls_as_tenant,
    rls_bypass,
)

pytestmark = pytest.mark.django_db(transaction=True)


# ── Policy verification tests ──────────────────────────────────────


class TestRLSPolicies:
    """Verify RLS is correctly configured on all protected tables."""

    def test_note_table_has_rls_enabled(self):
        assert_rls_enabled("notes_note")

    def test_note_table_has_policy(self):
        assert_rls_policy_exists("notes_note")

    def test_category_table_has_rls_enabled(self):
        assert_rls_enabled("notes_category")

    def test_category_table_has_policy(self):
        assert_rls_policy_exists("notes_category")


# ── Fail-closed behavior tests ─────────────────────────────────────


class TestFailClosed:
    """Verify that queries without RLS context return zero rows.

    These tests verify the **database-level** RLS enforcement (PostgreSQL
    returns zero rows when no GUC context is set). We temporarily disable
    ``STRICT_MODE`` so the ORM-level guard does not fire first -- this
    isolates the database-layer behavior.
    """

    @pytest.fixture(autouse=True)
    def _disable_strict_mode(self, settings):
        """Disable strict mode so database-layer RLS is tested in isolation.

        Clears the config cache so ``RLSTenantsConfig`` re-reads the
        overridden setting, and restores it after the test.
        """
        from django_rls_tenants.tenants.conf import rls_tenants_config

        old_cache = rls_tenants_config._config_cache  # noqa: SLF001
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "STRICT_MODE": False}
        rls_tenants_config._config_cache = None  # noqa: SLF001
        yield
        rls_tenants_config._config_cache = old_cache  # noqa: SLF001

    def test_notes_blocked_without_context(self, tenant_acme):
        from notes.models import Note

        # Seed a note so the table is not empty
        with rls_bypass():
            Note.objects.create(title="test", tenant=tenant_acme)

        assert_rls_blocks_without_context(Note)

    def test_categories_blocked_without_context(self, tenant_acme):
        from notes.models import Category

        with rls_bypass():
            Category.objects.create(name="test", tenant=tenant_acme)

        assert_rls_blocks_without_context(Category)


# ── Tenant isolation tests ──────────────────────────────────────────


class TestTenantIsolation:
    """Verify data isolation between tenants using rls_as_tenant."""

    def test_tenant_sees_only_own_notes(self, tenant_acme, tenant_globex):
        from notes.models import Note

        with rls_bypass():
            Note.objects.create(title="Acme note", tenant=tenant_acme)
            Note.objects.create(title="Globex note", tenant=tenant_globex)

        with rls_as_tenant(tenant_acme.pk):
            notes = list(Note.objects.values_list("title", flat=True))
            assert notes == ["Acme note"]

        with rls_as_tenant(tenant_globex.pk):
            notes = list(Note.objects.values_list("title", flat=True))
            assert notes == ["Globex note"]

    def test_tenant_sees_only_own_categories(self, tenant_acme, tenant_globex):
        from notes.models import Category

        with rls_bypass():
            Category.objects.create(name="Acme Cat", tenant=tenant_acme)
            Category.objects.create(name="Globex Cat", tenant=tenant_globex)

        with rls_as_tenant(tenant_acme.pk):
            cats = list(Category.objects.values_list("name", flat=True))
            assert cats == ["Acme Cat"]

    def test_admin_bypass_sees_all(self, tenant_acme, tenant_globex):
        from notes.models import Note

        with rls_bypass():
            Note.objects.create(title="Acme note", tenant=tenant_acme)
            Note.objects.create(title="Globex note", tenant=tenant_globex)
            assert Note.objects.count() == 2  # noqa: PLR2004 -- expected count

    def test_select_related_respects_tenant(self, tenant_acme, tenant_globex):
        """select_related() auto-propagates tenant filters to joined tables."""
        from notes.models import Category, Note

        with rls_bypass():
            acme_cat = Category.objects.create(name="Eng", tenant=tenant_acme)
            globex_cat = Category.objects.create(name="Eng", tenant=tenant_globex)
            Note.objects.create(title="A", tenant=tenant_acme, category=acme_cat)
            Note.objects.create(title="B", tenant=tenant_globex, category=globex_cat)

        with rls_as_tenant(tenant_acme.pk):
            notes = Note.objects.select_related("category").all()
            assert len(notes) == 1
            assert notes[0].category.name == "Eng"
            assert notes[0].category.tenant_id == tenant_acme.pk


# ── Strict mode tests ─────────────────────────────────────────────


class TestStrictMode:
    """Verify strict mode raises NoTenantContextError on unscoped queries.

    When ``STRICT_MODE=True`` in ``RLS_TENANTS``, querying an
    RLS-protected model without an active tenant context raises
    ``NoTenantContextError`` instead of silently returning zero rows.
    This catches accidental unscoped queries during development.
    """

    @pytest.fixture(autouse=True)
    def _ensure_strict_mode(self):
        """Clear config cache so STRICT_MODE=True is read from settings."""
        from django_rls_tenants.tenants.conf import rls_tenants_config

        rls_tenants_config._config_cache = None  # noqa: SLF001
        yield
        rls_tenants_config._config_cache = None  # noqa: SLF001

    def test_query_without_context_raises(self):
        """Querying without tenant context raises NoTenantContextError."""
        from notes.models import Note

        with pytest.raises(NoTenantContextError, match="strict mode"):
            Note.objects.count()

    def test_query_with_context_succeeds(self, tenant_acme):
        """Querying with tenant context works normally."""
        from notes.models import Note

        with rls_as_tenant(tenant_acme.pk):
            # Should not raise -- context is active
            assert Note.objects.count() == 0

    def test_admin_bypass_succeeds(self):
        """Admin bypass context is not blocked by strict mode."""
        from notes.models import Note

        with rls_bypass():
            # Should not raise -- admin context is active
            assert Note.objects.count() >= 0
