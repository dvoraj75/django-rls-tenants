"""Tests for django_rls_tenants.tenants.managers."""

from __future__ import annotations

import inspect
import itertools
import json
import warnings
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import django
import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import FieldError
from django.db import DatabaseError, DataError, NotSupportedError, connection, transaction
from django.db.models import Count, F, QuerySet, Sum
from django.db.models.signals import pre_delete
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.connection import ConnectionDoesNotExist

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants import managers
from django_rls_tenants.tenants.context import (
    admin_context,
    async_tenant_context,
    tenant_context,
)
from django_rls_tenants.tenants.managers import (
    _ITERATOR_CHUNK_SIZE,
    RLSManager,
    TenantQuerySet,
    _is_rls_protected,
    _resolve_related_model,
    _resolve_related_path,
)
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    get_rls_context_active,
    reset_current_tenant_id,
    set_current_tenant_id,
)
from tests.session_gucs import session_gucs
from tests.test_app.models import (
    Document,
    NoteRevision,
    Order,
    OrderItem,
    OrderNote,
    OrderNoteAttachment,
    Tenant,
    TenantUser,
)

pytestmark = pytest.mark.django_db


class TestForUserTenantUser:
    """Tests for TenantQuerySet.for_user() with tenant users."""

    def test_returns_only_tenant_rows(self, sample_orders, tenant_a_user, enforce_rls):
        """Tenant user sees only their own tenant's rows."""
        qs = Order.objects.for_user(tenant_a_user)
        products = list(qs.values_list("product", flat=True))
        assert sorted(products) == ["Widget A1", "Widget A2"]

    def test_does_not_return_other_tenant_rows(self, sample_orders, tenant_b_user, enforce_rls):
        """Tenant B user does not see Tenant A's rows."""
        qs = Order.objects.for_user(tenant_b_user)
        products = list(qs.values_list("product", flat=True))
        assert products == ["Gadget B1"]


class TestForUserAdmin:
    """Tests for TenantQuerySet.for_user() with admin users."""

    def test_returns_all_rows(self, sample_orders, admin_user, enforce_rls):
        """Admin user sees all tenants' rows."""
        qs = Order.objects.for_user(admin_user)
        assert qs.count() == 3


class TestForUserLazy:
    """Tests for lazy evaluation behavior."""

    def test_chainable(self, sample_orders, tenant_a_user, enforce_rls):
        """for_user() result is chainable with standard queryset methods."""
        qs = Order.objects.for_user(tenant_a_user).order_by("product")
        products = list(qs.values_list("product", flat=True))
        assert products == ["Widget A1", "Widget A2"]

    def test_filter_after_for_user(self, sample_orders, tenant_a_user, enforce_rls):
        """filter() after for_user() preserves the user reference."""
        qs = Order.objects.for_user(tenant_a_user).filter(amount__gte=Decimal("15.00"))
        products = list(qs.values_list("product", flat=True))
        assert products == ["Widget A2"]

    def test_lazy_evaluation(self, sample_orders, tenant_a_user, enforce_rls):
        """QuerySet from for_user() works when evaluated later (lazy).

        The GUCs must NOT be set when for_user() is called, only while the
        query runs (here count(), which bypasses _fetch_all()).
        """
        # Create queryset (no SQL executed yet)
        qs = Order.objects.for_user(tenant_a_user)
        # GUC should NOT be set yet -- for_user() only stores the user
        assert get_guc("rls.current_tenant") is None
        # Force evaluation
        count = qs.count()
        assert count == 2
        # GUC should be cleared after evaluation
        assert get_guc("rls.current_tenant") is None

    def test_clone_propagates_user(self, sample_orders, tenant_a_user, enforce_rls):
        """Cloned querysets (from .filter(), .order_by()) keep the user ref."""
        qs1 = Order.objects.for_user(tenant_a_user)
        qs2 = qs1.filter(product="Widget A1")
        assert qs2._rls_user is tenant_a_user
        assert list(qs2.values_list("product", flat=True)) == ["Widget A1"]


class TestFetchAllGucCleanupOnException:
    """Tests that GUC variables are cleaned up even when _fetch_all raises."""

    def test_guc_cleared_after_db_error(self, tenant_a_user, monkeypatch):
        """GUC variables are cleared even when the underlying query raises.

        This verifies the try/finally in _fetch_all properly cleans up
        GUC state on failure, preventing stale tenant context.
        """
        qs = Order.objects.for_user(tenant_a_user)

        def exploding_fetch_all(self_inner):
            raise RuntimeError("Simulated DB error")

        import django.db.models.query  # noqa: PLC0415

        monkeypatch.setattr(django.db.models.query.QuerySet, "_fetch_all", exploding_fetch_all)

        with pytest.raises(RuntimeError, match="Simulated DB error"):
            list(qs)  # trigger _fetch_all

        # GUC should be cleared despite the error
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None


class TestAutoScope:
    """Tests for automatic query scoping via ContextVar state."""

    def test_auto_scope_adds_filter_in_tenant_context(self, sample_orders, tenant_a):
        """get_queryset() adds tenant filter when tenant context is active."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all()
            # The queryset should contain a tenant_id filter
            sql = str(qs.query)
            assert "tenant_id" in sql
            products = list(qs.values_list("product", flat=True))
            assert sorted(products) == ["Widget A1", "Widget A2"]

    def test_auto_scope_no_filter_without_context(self, sample_orders):
        """get_queryset() adds no filter when no context is active."""
        assert get_current_tenant_id() is None
        qs = Order.objects.all()
        sql = str(qs.query)
        assert "WHERE" not in sql

    def test_auto_scope_no_filter_in_admin_context(self, sample_orders):
        """get_queryset() adds no filter when admin context is active."""
        with admin_context():
            qs = Order.objects.all()
            sql = str(qs.query)
            assert "WHERE" not in sql

    def test_auto_scope_with_chained_filter(self, sample_orders, tenant_a):
        """Auto-scope works with additional .filter() calls."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.filter(amount__gte=Decimal("15.00"))
            products = list(qs.values_list("product", flat=True))
            assert products == ["Widget A2"]

    def test_auto_scope_changes_on_context_switch(self, sample_orders, tenant_a, tenant_b):
        """Auto-scope follows the active tenant context."""
        with tenant_context(tenant_a.pk):
            a_products = sorted(Order.objects.values_list("product", flat=True))
            assert a_products == ["Widget A1", "Widget A2"]

        with tenant_context(tenant_b.pk):
            b_products = list(Order.objects.values_list("product", flat=True))
            assert b_products == ["Gadget B1"]

    def test_for_user_still_works_with_auto_scope(
        self, sample_orders, tenant_a_user, tenant_a, enforce_rls
    ):
        """for_user() still works correctly even when auto-scope is also active.

        Both add a WHERE tenant_id = X clause. The redundancy is by design
        for defense-in-depth; the double equality check cost is negligible.
        """
        with tenant_context(tenant_a.pk):
            qs = Order.objects.for_user(tenant_a_user)
            products = sorted(qs.values_list("product", flat=True))
            assert products == ["Widget A1", "Widget A2"]

    def test_auto_scope_with_manual_state(self, sample_orders, tenant_a):
        """Auto-scope works when state is set manually (not via context manager)."""
        token = set_current_tenant_id(tenant_a.pk)
        try:
            qs = Order.objects.all()
            sql = str(qs.query)
            assert "tenant_id" in sql
        finally:
            reset_current_tenant_id(token)


class TestFetchAllContextVar:
    """Tests for ContextVar management in _fetch_all()."""

    def test_fetch_all_sets_contextvar_for_tenant_user(self, sample_orders, tenant_a_user):
        """_fetch_all() sets ContextVar during evaluation for prefetch support."""
        qs = Order.objects.for_user(tenant_a_user)
        # Before evaluation, ContextVar should be None
        assert get_current_tenant_id() is None
        # Evaluate
        list(qs)
        # After evaluation, ContextVar should be restored to None
        assert get_current_tenant_id() is None

    def test_fetch_all_clears_contextvar_for_admin(self, sample_orders, admin_user):
        """_fetch_all() sets ContextVar to None for admin users."""
        qs = Order.objects.for_user(admin_user)
        assert get_current_tenant_id() is None
        list(qs)
        assert get_current_tenant_id() is None

    def test_fetch_all_restores_contextvar_on_exception(self, tenant_a_user, monkeypatch):
        """ContextVar is restored even when _fetch_all raises."""
        qs = Order.objects.for_user(tenant_a_user)

        import django.db.models.query  # noqa: PLC0415

        def exploding_fetch(self_inner):
            # Verify ContextVar IS set during evaluation
            assert get_current_tenant_id() == tenant_a_user.rls_tenant_id
            raise RuntimeError("boom")

        monkeypatch.setattr(django.db.models.query.QuerySet, "_fetch_all", exploding_fetch)

        with pytest.raises(RuntimeError, match="boom"):
            list(qs)

        # Must be restored after exception
        assert get_current_tenant_id() is None


class TestGetActiveTenantId:
    """Tests for TenantQuerySet._get_active_tenant_id()."""

    def test_returns_none_without_context(self, sample_orders):
        """Returns None when no tenant context is active."""
        qs = Order.objects.all()
        assert qs._get_active_tenant_id() is None

    def test_returns_contextvar_value(self, sample_orders, tenant_a):
        """Returns ContextVar value when tenant context is active."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all()
            assert qs._get_active_tenant_id() == tenant_a.pk

    def test_returns_rls_user_tenant_id(self, sample_orders, tenant_a_user):
        """Returns _rls_user.rls_tenant_id when for_user() is used."""
        qs = Order.objects.for_user(tenant_a_user)
        assert qs._get_active_tenant_id() == tenant_a_user.rls_tenant_id

    def test_returns_none_for_admin_user(self, sample_orders, admin_user):
        """Returns None for admin users (no filter should be applied)."""
        qs = Order.objects.for_user(admin_user)
        assert qs._get_active_tenant_id() is None

    def test_contextvar_takes_precedence(self, sample_orders, tenant_a_user, tenant_b):
        """ContextVar takes precedence over _rls_user."""
        qs = Order.objects.for_user(tenant_a_user)
        with tenant_context(tenant_b.pk):
            # ContextVar (tenant_b) should take precedence over _rls_user (tenant_a)
            assert qs._get_active_tenant_id() == tenant_b.pk


class TestIsRlsProtected:
    """Tests for the _is_rls_protected() helper."""

    def test_order_is_protected(self):
        """Order model (RLSProtectedModel) is detected as RLS-protected."""
        assert _is_rls_protected(Order) is True

    def test_tenant_is_not_protected(self):
        """Tenant model (plain model) is not RLS-protected."""
        assert _is_rls_protected(Tenant) is False


class TestPrepareTenantInModelData:
    """Tests for RLSManager.prepare_tenant_in_model_data()."""

    def test_resolves_raw_id(self, tenant_a, tenant_a_user):
        """Raw tenant ID (int) sets FK column directly (no model fetch)."""
        data = {"tenant": tenant_a.pk, "product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        # The FK field is replaced by the FK column (tenant -> tenant_id)
        assert "tenant" not in data
        assert data["tenant_id"] == tenant_a.pk

    def test_skips_model_instance(self, tenant_a, tenant_a_user):
        """Already a Tenant instance -- no change."""
        data = {"tenant": tenant_a, "product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        assert data["tenant"] is tenant_a

    def test_skips_missing_key(self, tenant_a_user):
        """No tenant key in data -- no error."""
        data = {"product": "Widget"}
        Order.objects.prepare_tenant_in_model_data(data, as_user=tenant_a_user)
        assert "tenant" not in data


class TestSelectRelated:
    """Tests for TenantQuerySet.select_related() tenant propagation."""

    def test_select_related_false_does_not_crash(self, sample_orders, tenant_a):
        """select_related(False) must not crash (clears select_related)."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all().select_related("tenant").select_related(False)
            # Should work without AttributeError
            assert list(qs) is not None

    @pytest.mark.parametrize("via_manager", [False, True])
    def test_select_related_no_args_warns_at_caller_on_django_61(
        self, sample_orders, tenant_a, via_manager
    ):
        """select_related() with no arguments selects everything, deprecated on 6.1+.

        Django 6.1 deprecates the no-argument form. The warning must name this
        file (the caller), not ``managers.py``, whether the call goes through the
        manager proxy or a queryset. Older Django must not warn at all.
        """
        with tenant_context(tenant_a.pk):
            source = Order.objects if via_manager else Order.objects.all()
            if django.VERSION >= (6, 1):
                with pytest.warns(
                    (DeprecationWarning, PendingDeprecationWarning),
                    match=r"select_related\(\) with no arguments",
                ) as rec:
                    qs = source.select_related()
                assert len(rec) == 1
                assert rec[0].filename == __file__
            else:
                with warnings.catch_warnings(record=True) as rec:
                    warnings.simplefilter("always")
                    qs = source.select_related()
                deprecations = [
                    w
                    for w in rec
                    if issubclass(w.category, (DeprecationWarning, PendingDeprecationWarning))
                ]
                assert deprecations == []
            assert qs.query.select_related is True
            # Auto-scope adds tenant_id filter, but no extra join filters
            assert "tenant_id" in str(qs.query)
            assert {order.tenant_id for order in qs} == {tenant_a.pk}

    def test_select_related_no_args_silent_for_django_internal_caller(
        self, sample_orders, tenant_a
    ):
        """A caller inside Django (the admin changelist) gets no warning from us.

        Django deprecates ``list_select_related=True`` itself; warning again from
        the queryset would duplicate it. The call is compiled into a module whose
        ``__name__`` is ``django.contrib.admin.views.main``, which is what
        ``warn_about_external_use`` inspects to tell Django's frames apart.
        """
        namespace = {"__name__": "django.contrib.admin.views.main", "Order": Order}
        code = compile("qs = Order.objects.all().select_related()", "fake_admin.py", "exec")
        with tenant_context(tenant_a.pk), warnings.catch_warnings(record=True) as rec:
            warnings.simplefilter("always")
            exec(code, namespace)  # noqa: S102  -- fixed source, simulates a Django caller
        assert rec == []
        assert namespace["qs"].query.select_related is True

    def test_select_related_no_args_after_values_raises(self, sample_orders, tenant_a):
        """The no-argument form keeps Django's guard against values() querysets."""
        with (
            tenant_context(tenant_a.pk),
            pytest.raises(TypeError, match=r"after \.values\(\)"),
        ):
            Order.objects.values("pk").select_related()

    def test_select_related_no_args_on_combined_query_raises(self, sample_orders, tenant_a):
        """The no-argument form keeps Django's guard against union() querysets."""
        with tenant_context(tenant_a.pk):
            combined = Order.objects.all().union(Order.objects.all())
            with pytest.raises(NotSupportedError, match="select_related"):
                combined.select_related()

    def test_select_related_non_rls_model_no_extra_filter(self, sample_orders, tenant_a):
        """select_related('tenant') to a non-RLS model does NOT add extra filter."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all().select_related("tenant")
            sql = str(qs.query)
            # Auto-scope filter is present (from get_queryset)
            assert "tenant_id" in sql
            # But no extra filter like tenant__tenant_id should be added
            # because Tenant is not RLS-protected
            assert "tenant__tenant_id" not in sql

    def test_select_related_rls_model_adds_filter(self, sample_order_items, tenant_a):
        """select_related('order') on OrderItem adds tenant filter on joined Order."""
        with tenant_context(tenant_a.pk):
            qs = OrderItem.objects.all().select_related("order")
            sql = str(qs.query)
            # Auto-scope on OrderItem itself
            assert "tenant_id" in sql
            # Extra filter on the joined Order table
            assert "order__tenant_id" in sql or "order" in sql

    def test_select_related_rls_model_returns_correct_data(self, sample_order_items, tenant_a):
        """select_related('order') returns only tenant A's items with orders."""
        with tenant_context(tenant_a.pk):
            items = list(OrderItem.objects.all().select_related("order"))
            descriptions = sorted(item.description for item in items)
            assert descriptions == ["Part A1-1", "Part A2-1"]
            # Verify the joined order is accessible
            for item in items:
                assert item.order is not None
                assert item.order.tenant_id == tenant_a.pk

    def test_select_related_dotted_path(self, sample_order_items, tenant_a):
        """select_related('order__tenant') follows multi-level path."""
        with tenant_context(tenant_a.pk):
            qs = OrderItem.objects.all().select_related("order__tenant")
            # Should not crash; order is RLS-protected, tenant is not
            items = list(qs)
            assert len(items) == 2

    def test_select_related_invalid_field_ignored(self, sample_orders, tenant_a):
        """select_related('nonexistent') is handled by Django (not our code)."""
        with tenant_context(tenant_a.pk):
            # Django validates fields at evaluation time. Our override
            # calls _resolve_related_model which returns None for invalid paths.
            qs = Order.objects.all().select_related("nonexistent")
            # _resolve_related_model returns None, so no extra filter
            # Django itself will raise FieldError when evaluating the queryset
            with pytest.raises(FieldError, match="Invalid field name"):
                list(qs)

    def test_select_related_no_context_skips_filter(self, sample_order_items):
        """select_related() without tenant context does not add extra filters."""
        assert get_current_tenant_id() is None
        qs = OrderItem.objects.all().select_related("order")
        sql = str(qs.query)
        # No auto-scope, no extra join filters
        assert "WHERE" not in sql

    def test_select_related_admin_context_skips_filter(self, sample_order_items):
        """select_related() in admin context does not add extra filters."""
        with admin_context():
            qs = OrderItem.objects.all().select_related("order")
            sql = str(qs.query)
            # Admin context: no auto-scope filter
            assert "WHERE" not in sql

    def test_select_related_with_for_user(self, sample_order_items, tenant_a_user, enforce_rls):
        """select_related() works correctly with for_user()."""
        qs = OrderItem.objects.for_user(tenant_a_user).select_related("order")
        items = list(qs)
        descriptions = sorted(item.description for item in items)
        assert descriptions == ["Part A1-1", "Part A2-1"]

    def test_select_related_nullable_fk_preserves_null_rows(self, sample_order_notes, tenant_a):
        """select_related() on nullable FK preserves rows where FK is NULL."""
        with tenant_context(tenant_a.pk):
            notes = list(OrderNote.objects.select_related("order"))
            bodies = sorted(n.body for n in notes)
            assert bodies == ["Linked to order A1", "No order (orphan)"]

    def test_select_related_nullable_fk_linked_row_has_order(self, sample_order_notes, tenant_a):
        """select_related() on nullable FK still resolves non-NULL FKs."""
        with tenant_context(tenant_a.pk):
            notes = list(OrderNote.objects.select_related("order"))
            linked = [n for n in notes if n.order is not None]
            assert len(linked) == 1
            assert linked[0].order.tenant_id == tenant_a.pk

    def test_select_related_nullable_fk_orphan_row_has_none(self, sample_order_notes, tenant_a):
        """select_related() on nullable FK returns None for NULL FK rows."""
        with tenant_context(tenant_a.pk):
            notes = list(OrderNote.objects.select_related("order"))
            orphans = [n for n in notes if n.order is None]
            assert len(orphans) == 1
            assert orphans[0].body == "No order (orphan)"

    def test_select_related_nullable_fk_isolation(self, sample_order_notes, tenant_a, tenant_b):
        """select_related() on nullable FK still isolates tenants."""
        with tenant_context(tenant_a.pk):
            a_notes = list(OrderNote.objects.select_related("order"))
            assert len(a_notes) == 2

        with tenant_context(tenant_b.pk):
            b_notes = list(OrderNote.objects.select_related("order"))
            assert len(b_notes) == 1
            assert b_notes[0].body == "Linked to order B1"


class TestSelectRelatedNestedNullable:
    """``select_related()`` on nested paths with a nullable hop (#79).

    Before the fix the ``__isnull`` escape hatch was only added when the
    *full dotted path* resolved via ``get_field()``, which never happens for
    ``a__b`` paths, so the tenant filter on the leaf forced an INNER JOIN and
    silently dropped every row whose chain had a NULL at any hop.
    """

    @pytest.fixture(autouse=True, params=[False, True], ids=["orm", "rls"])
    def _rls_enforcement(self, request, rls_role):
        """Exercise ORM filtering both with and without database RLS enforcement."""
        if request.param:
            request.getfixturevalue("enforce_rls")

    def test_nullable_intermediate_and_nullable_leaf_keeps_all_rows(
        self, sample_nested_rows, tenant_a
    ):
        """``note__order``: NULL at the first hop and NULL at the leaf both survive."""
        with tenant_context(tenant_a.pk):
            rows = list(OrderNoteAttachment.objects.select_related("note__order"))
        assert sorted(r.filename for r in rows) == ["full.txt", "leaf-null.txt", "orphan.txt"]
        by_name = {r.filename: r for r in rows}
        assert by_name["orphan.txt"].note is None
        assert by_name["leaf-null.txt"].note is not None
        assert by_name["leaf-null.txt"].note.order is None
        assert by_name["full.txt"].note.order.tenant_id == tenant_a.pk

    def test_nullable_intermediate_and_non_null_leaf_keeps_all_rows(
        self, sample_nested_rows, tenant_a
    ):
        """``item__order``: a nullable first hop alone must keep the LEFT JOIN.

        ``OrderItem.order`` is NOT nullable, so a leaf-only nullability check
        would still drop the two attachments without an item.
        """
        with tenant_context(tenant_a.pk):
            rows = list(OrderNoteAttachment.objects.select_related("item__order"))
        assert sorted(r.filename for r in rows) == ["full.txt", "leaf-null.txt", "orphan.txt"]
        by_name = {r.filename: r for r in rows}
        assert by_name["orphan.txt"].item is None
        assert by_name["full.txt"].item.order.tenant_id == tenant_a.pk

    def test_non_null_intermediate_and_nullable_leaf_keeps_all_rows(
        self, sample_nested_rows, tenant_a
    ):
        """``note__order`` from ``NoteRevision``: only the leaf is nullable."""
        with tenant_context(tenant_a.pk):
            rows = list(NoteRevision.objects.select_related("note__order"))
        assert sorted(r.summary for r in rows) == ["full", "leaf-null"]
        by_name = {r.summary: r for r in rows}
        assert by_name["leaf-null"].note.order is None
        assert by_name["full"].note.order.tenant_id == tenant_a.pk

    def test_fully_non_null_path_uses_inner_join(self, sample_nested_rows, tenant_a):
        """``item__order`` from ``NoteRevision``: no nullable hop, INNER JOIN is right."""
        with tenant_context(tenant_a.pk):
            qs = NoteRevision.objects.select_related("item__order")
            sql = str(qs.query)
            rows = list(qs)
        assert sorted(r.summary for r in rows) == ["full", "leaf-null"]
        assert "LEFT OUTER JOIN" not in sql
        assert "INNER JOIN" in sql
        assert '"test_order"."tenant_id"' in sql

    def test_nested_nullable_sql_shape(self, sample_nested_rows, tenant_a):
        """The leaf keeps the tenant predicate and the joins stay LEFT OUTER."""
        with tenant_context(tenant_a.pk):
            sql = str(OrderNoteAttachment.objects.select_related("note__order").query)
        assert sql.count("LEFT OUTER JOIN") == 2
        assert "INNER JOIN" not in sql
        assert '"test_order"."tenant_id"' in sql
        assert "IS NULL" in sql

    def test_nested_nullable_isolation(self, sample_nested_rows, tenant_a, tenant_b):
        """The preserved rows are still scoped to the active tenant."""
        with tenant_context(tenant_b.pk):
            atts = list(OrderNoteAttachment.objects.select_related("note__order"))
            revs = list(NoteRevision.objects.select_related("note__order"))
        assert [a.filename for a in atts] == ["b.txt"]
        assert atts[0].note.order.tenant_id == tenant_b.pk
        assert [r.summary for r in revs] == ["b"]

    def test_nested_nullable_excludes_cross_tenant_leaf(
        self, sample_nested_rows, sample_orders, tenant_a
    ):
        """A root in the active tenant must not expose another tenant's leaf."""
        with admin_context():
            cross_tenant_note = OrderNote.objects.create(
                tenant=tenant_a,
                order=sample_orders["b1"],
                body="Cross-tenant order",
            )
            attachment = OrderNoteAttachment.objects.create(
                tenant=tenant_a,
                note=cross_tenant_note,
                filename="cross-tenant.txt",
            )
        with tenant_context(tenant_a.pk):
            # The root and intermediate object are visible. Only the predicate
            # on the joined leaf can exclude this attachment in the ORM case.
            assert OrderNoteAttachment.objects.filter(pk=attachment.pk).exists()
            rows = list(OrderNoteAttachment.objects.select_related("note__order"))
        assert sorted(r.filename for r in rows) == ["full.txt", "leaf-null.txt", "orphan.txt"]

    def test_nested_nullable_with_for_user(self, sample_nested_rows, tenant_a_user):
        """Deferred user scoping preserves missing relations at either hop."""
        rows = list(
            OrderNoteAttachment.objects.for_user(tenant_a_user).select_related("note__order")
        )
        assert sorted(r.filename for r in rows) == ["full.txt", "leaf-null.txt", "orphan.txt"]

    def test_nested_path_to_non_rls_leaf_adds_no_filter(self, sample_nested_rows, tenant_a):
        """``note__tenant`` ends at a non-RLS model: no extra predicate, no crash."""
        with tenant_context(tenant_a.pk):
            qs = OrderNoteAttachment.objects.select_related("note__tenant")
            # Compare the predicates directly: Django renders table aliases,
            # not lookup strings such as "tenant__tenant_id", in SQL.
            assert qs.query.where == OrderNoteAttachment.objects.all().query.where
            rows = list(qs)
        assert len(rows) == 3

    def test_single_segment_behaviour_unchanged(self, sample_order_notes, tenant_a):
        """The pre-existing single-hop nullable case still gets its ``IS NULL`` clause."""
        with tenant_context(tenant_a.pk):
            sql = str(OrderNote.objects.select_related("order").query)
        assert "LEFT OUTER JOIN" in sql
        assert "IS NULL" in sql


class TestResolveRelatedPath:
    """Tests for the _resolve_related_path() helper (model + nullability)."""

    def test_single_non_null_fk(self):
        assert _resolve_related_path(OrderItem, "order") == (Order, False)

    def test_single_nullable_fk(self):
        assert _resolve_related_path(OrderNote, "order") == (Order, True)

    def test_nullable_intermediate_non_null_leaf(self):
        assert _resolve_related_path(OrderNoteAttachment, "item__order") == (Order, True)

    def test_non_null_intermediate_nullable_leaf(self):
        assert _resolve_related_path(NoteRevision, "note__order") == (Order, True)

    def test_fully_non_null_chain(self):
        assert _resolve_related_path(NoteRevision, "item__order") == (Order, False)

    def test_both_hops_nullable(self):
        assert _resolve_related_path(OrderNoteAttachment, "note__order") == (Order, True)

    def test_invalid_path_returns_none(self):
        assert _resolve_related_path(OrderNoteAttachment, "note__nope") == (None, False)

    def test_non_relation_field_returns_none(self):
        assert _resolve_related_path(Order, "product") == (None, False)


class TestResolveRelatedModel:
    """Tests for the _resolve_related_model() helper."""

    def test_single_field_fk(self):
        """Resolves a single FK field to its target model."""
        result = _resolve_related_model(Order, "tenant")
        assert result is Tenant

    def test_single_field_fk_to_rls_model(self):
        """Resolves FK from OrderItem to Order (both RLS-protected)."""
        result = _resolve_related_model(OrderItem, "order")
        assert result is Order

    def test_dotted_path(self):
        """Resolves a dotted path across multiple relations."""
        result = _resolve_related_model(OrderItem, "order__tenant")
        assert result is Tenant

    def test_invalid_field_returns_none(self):
        """Returns None for a non-existent field name."""
        result = _resolve_related_model(Order, "nonexistent")
        assert result is None

    def test_invalid_second_segment_returns_none(self):
        """Returns None for an invalid second segment in a dotted path."""
        result = _resolve_related_model(OrderItem, "order__nonexistent")
        assert result is None

    def test_non_relation_field_returns_none(self):
        """Returns None for a non-relation field (e.g., CharField)."""
        result = _resolve_related_model(Order, "product")
        assert result is None


# ---- Strict mode tests ----


def _strict_settings():
    """Return override_settings context for STRICT_MODE=True."""
    return override_settings(
        RLS_TENANTS={
            "TENANT_MODEL": "test_app.Tenant",
            "STRICT_MODE": True,
        },
    )


@pytest.fixture
def _strict_mode():
    """Enable strict mode for the test (the config reloads on ``setting_changed``)."""
    with _strict_settings():
        yield


class TestStrictModeDisabled:
    """Tests that strict mode off (default) preserves existing behavior."""

    def test_no_context_returns_empty(self, sample_orders, enforce_rls):
        """Without context and strict mode off, queries return empty (fail-closed)."""
        assert get_current_tenant_id() is None
        qs = Order.objects.all()
        # No strict mode error: RLS hides every row instead.
        assert list(qs) == []


class TestStrictModeRaises:
    """Tests that strict mode raises NoTenantContextError on evaluation."""

    @pytest.mark.usefixtures("_strict_mode")
    def test_fetch_all_raises(self, sample_orders):
        """list(qs) raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            list(qs)

    @pytest.mark.usefixtures("_strict_mode")
    def test_count_raises(self, sample_orders):
        """qs.count() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.count()

    @pytest.mark.usefixtures("_strict_mode")
    def test_exists_raises(self, sample_orders):
        """qs.exists() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.exists()

    @pytest.mark.usefixtures("_strict_mode")
    def test_aggregate_raises(self, sample_orders):
        """qs.aggregate() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.aggregate(total=Sum("amount"))

    @pytest.mark.usefixtures("_strict_mode")
    def test_update_raises(self, sample_orders):
        """qs.update() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.update(product="X")

    @pytest.mark.usefixtures("_strict_mode")
    def test_delete_raises(self, sample_orders):
        """qs.delete() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.delete()

    @pytest.mark.usefixtures("_strict_mode")
    def test_get_raises(self, sample_orders):
        """qs.get() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.get(pk=sample_orders["a1"].pk)

    @pytest.mark.usefixtures("_strict_mode")
    def test_first_raises(self, sample_orders):
        """qs.first() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.first()

    @pytest.mark.usefixtures("_strict_mode")
    def test_last_raises(self, sample_orders):
        """qs.last() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.last()

    @pytest.mark.usefixtures("_strict_mode")
    def test_iterator_raises(self, sample_orders):
        """qs.iterator() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            list(qs.iterator())

    @pytest.mark.usefixtures("_strict_mode")
    def test_bulk_create_raises(self, sample_orders, tenant_a):
        """qs.bulk_create() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.bulk_create([Order(product="X", amount=1, tenant=tenant_a)])

    @pytest.mark.usefixtures("_strict_mode")
    def test_bulk_update_raises(self, sample_orders):
        """qs.bulk_update() raises NoTenantContextError without context."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="strict mode"):
            qs.bulk_update([], fields=["product"])

    @pytest.mark.usefixtures("_strict_mode")
    def test_error_message_contains_model_name(self, sample_orders):
        """Error message includes the model name for debugging."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError, match="Order"):
            list(qs)

    @pytest.mark.usefixtures("_strict_mode")
    def test_error_carries_actionable_hint(self, sample_orders):
        """The bare message states the problem; .hint covers the fixes and STRICT_MODE."""
        qs = Order.objects.all()
        with pytest.raises(NoTenantContextError) as exc_info:
            list(qs)
        # The fix-it guidance moved out of the message into the hint.
        assert exc_info.value.message.endswith("without an active RLS context.")
        hint = exc_info.value.hint
        assert hint is not None
        assert "tenant_context(" in hint
        assert "STRICT_MODE=False" in hint


class TestStrictModeAllowsContext:
    """Tests that strict mode does NOT raise when context is properly set."""

    @pytest.mark.usefixtures("_strict_mode")
    def test_tenant_context_allows_query(self, sample_orders, tenant_a):
        """No error when tenant_context() is active."""
        with tenant_context(tenant_a.pk):
            products = list(Order.objects.values_list("product", flat=True))
            assert sorted(products) == ["Widget A1", "Widget A2"]

    @pytest.mark.usefixtures("_strict_mode")
    def test_admin_context_allows_query(self, sample_orders):
        """No error when admin_context() is active."""
        with admin_context():
            count = Order.objects.count()
            assert count == 3

    @pytest.mark.usefixtures("_strict_mode")
    def test_for_user_allows_query(self, sample_orders, tenant_a_user, enforce_rls):
        """No error when for_user() was called."""
        qs = Order.objects.for_user(tenant_a_user)
        products = list(qs.values_list("product", flat=True))
        assert sorted(products) == ["Widget A1", "Widget A2"]

    @pytest.mark.usefixtures("_strict_mode")
    def test_middleware_allows_query(self, sample_orders, tenant_a_user):
        """No error when middleware has set context."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = MagicMock()
        request.user = tenant_a_user
        mw.process_request(request)
        try:
            products = list(Order.objects.values_list("product", flat=True))
            assert sorted(products) == ["Widget A1", "Widget A2"]
        finally:
            mw.process_response(request, MagicMock())

    @pytest.mark.usefixtures("_strict_mode")
    def test_all_guarded_methods_pass_with_context(self, sample_orders, tenant_a):
        """All guarded methods work within tenant_context()."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.all()
            # These should all succeed without raising
            list(qs)
            Order.objects.all().count()
            Order.objects.all().exists()
            Order.objects.all().first()
            Order.objects.all().last()
            list(Order.objects.all().iterator())


class TestStrictModeContextVarLifecycle:
    """Tests that _rls_context_active is properly set and restored."""

    def test_tenant_context_sets_active(self, db, tenant_a):
        """tenant_context() sets _rls_context_active to True."""
        assert get_rls_context_active() is False
        with tenant_context(tenant_a.pk):
            assert get_rls_context_active() is True
        assert get_rls_context_active() is False

    def test_admin_context_sets_active(self, db):
        """admin_context() sets _rls_context_active to True."""
        assert get_rls_context_active() is False
        with admin_context():
            assert get_rls_context_active() is True
        assert get_rls_context_active() is False

    def test_nested_contexts_restore_active_flag(self, db, tenant_a, tenant_b):
        """Nested contexts properly restore the active flag."""
        assert get_rls_context_active() is False
        with tenant_context(tenant_a.pk):
            assert get_rls_context_active() is True
            with admin_context():
                assert get_rls_context_active() is True
            assert get_rls_context_active() is True
        assert get_rls_context_active() is False

    def test_active_flag_restored_on_exception(self, db, tenant_a):
        """_rls_context_active is restored when an exception occurs."""
        assert get_rls_context_active() is False
        with pytest.raises(RuntimeError, match="boom"), tenant_context(tenant_a.pk):
            raise RuntimeError("boom")
        assert get_rls_context_active() is False

    def test_middleware_sets_and_clears_active(self, db, tenant_a_user):
        """Middleware sets active on request and clears on response."""
        assert get_rls_context_active() is False
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = MagicMock()
        request.user = tenant_a_user
        mw.process_request(request)
        assert get_rls_context_active() is True
        mw.process_response(request, MagicMock())
        assert get_rls_context_active() is False

    def test_middleware_clears_active_on_exception(self, db, tenant_a_user):
        """Middleware clears active flag via process_exception."""
        assert get_rls_context_active() is False
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = MagicMock()
        request.user = tenant_a_user
        mw.process_request(request)
        assert get_rls_context_active() is True
        mw.process_exception(request, RuntimeError("view error"))
        assert get_rls_context_active() is False


# ---- for_user() scope on every method that runs SQL (#131) ----
#
# Every test below that checks rows runs under enforce_rls: a superuser
# connection bypasses RLS, so there a for_user() test passes whether or not
# the GUCs were set. Each test also builds a fresh queryset; an evaluated one
# answers count() and exists() from its result cache and would hide a
# missing scope.


@pytest.fixture(params=["admin", "tenant_a"])
def scoped_user(request, admin_user, tenant_a_user):
    """A ``for_user()`` user and how many of the three sample orders RLS shows it."""
    if request.param == "admin":
        return admin_user, 3
    return tenant_a_user, 2


@pytest.fixture(params=[False, True], ids=["set_config", "set_local"])
def guc_mode(request, settings):
    """Run the test in both GUC modes.

    pytest-django wraps each test in a transaction, which ``SET LOCAL`` needs.
    """
    settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": request.param}
    return request.param


async def _collect(aiterable):
    """Drain an async iterable into a list."""
    return [item async for item in aiterable]


def _explain_rows(plan_json):
    """Rows the top plan node returned, from ``EXPLAIN (ANALYZE, FORMAT JSON)`` output."""
    (plan,) = json.loads(plan_json)
    return plan["Plan"]["Actual Rows"]


def _assert_no_rls_context():
    """Neither the connection nor the tenant state carries a context."""
    assert get_guc("rls.is_admin") is None
    assert get_guc("rls.current_tenant") is None
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


def _new_order(tenant_id, **fields):
    """An unsaved order for ``tenant_id``."""
    return Order(product="New", amount=Decimal(1), tenant_id=tenant_id, **fields)


_READ_MEASURES = [
    pytest.param(lambda qs: len(list(qs)), id="iteration"),
    pytest.param(lambda qs: len(list(qs.values_list("id", flat=True))), id="values_list"),
    pytest.param(lambda qs: qs.count(), id="count"),
    pytest.param(lambda qs: qs.aggregate(n=Count("id"))["n"], id="aggregate"),
    pytest.param(lambda qs: len(list(qs.iterator())), id="iterator"),
    pytest.param(lambda qs: len(list(qs.iterator(chunk_size=1))), id="iterator_one_row_chunks"),
    pytest.param(lambda qs: len(qs.in_bulk()), id="in_bulk"),
    pytest.param(
        lambda qs: _explain_rows(qs.explain(format="json", analyze=True)),
        id="explain_analyze",
    ),
    pytest.param(lambda qs: len(async_to_sync(_collect)(qs)), id="async_for"),
    pytest.param(lambda qs: async_to_sync(qs.acount)(), id="acount"),
    pytest.param(lambda qs: async_to_sync(qs.aaggregate)(n=Count("id"))["n"], id="aaggregate"),
    pytest.param(lambda qs: len(async_to_sync(_collect)(qs.aiterator())), id="aiterator"),
    pytest.param(
        lambda qs: len(async_to_sync(_collect)(qs.aiterator(chunk_size=1))),
        id="aiterator_one_row_chunks",
    ),
    pytest.param(lambda qs: len(async_to_sync(qs.ain_bulk)()), id="ain_bulk"),
    pytest.param(
        lambda qs: _explain_rows(async_to_sync(qs.aexplain)(format="json", analyze=True)),
        id="aexplain_analyze",
    ),
]

_PRESENCE_CHECKS = [
    pytest.param(lambda qs, row: qs.exists(), id="exists"),
    pytest.param(lambda qs, row: async_to_sync(qs.aexists)(), id="aexists"),
    pytest.param(lambda qs, row: qs.contains(row), id="contains"),
    pytest.param(lambda qs, row: async_to_sync(qs.acontains)(row), id="acontains"),
    pytest.param(lambda qs, row: qs.get(pk=row.pk) == row, id="get"),
    pytest.param(lambda qs, row: async_to_sync(qs.aget)(pk=row.pk) == row, id="aget"),
    pytest.param(lambda qs, row: qs.first() is not None, id="first"),
    pytest.param(lambda qs, row: async_to_sync(qs.afirst)() is not None, id="afirst"),
    pytest.param(lambda qs, row: async_to_sync(qs.alast)() is not None, id="alast"),
    pytest.param(lambda qs, row: async_to_sync(qs.alatest)("pk") is not None, id="alatest"),
]

_INSERTS = [
    pytest.param(lambda qs, **fields: qs.create(**fields), id="create"),
    pytest.param(lambda qs, **fields: async_to_sync(qs.acreate)(**fields), id="acreate"),
    pytest.param(lambda qs, **fields: qs.bulk_create([Order(**fields)])[0], id="bulk_create"),
    pytest.param(
        lambda qs, **fields: async_to_sync(qs.abulk_create)([Order(**fields)])[0],
        id="abulk_create",
    ),
    pytest.param(
        lambda qs, product, **fields: qs.get_or_create(product=product, defaults=fields)[0],
        id="get_or_create",
    ),
    pytest.param(
        lambda qs, product, **fields: async_to_sync(qs.aget_or_create)(
            product=product,
            defaults=fields,
        )[0],
        id="aget_or_create",
    ),
    pytest.param(
        lambda qs, product, **fields: qs.update_or_create(product=product, defaults=fields)[0],
        id="update_or_create",
    ),
    pytest.param(
        lambda qs, product, **fields: async_to_sync(qs.aupdate_or_create)(
            product=product,
            defaults=fields,
        )[0],
        id="aupdate_or_create",
    ),
]


class TestForUserScopeReads:
    """Every read method applies the ``for_user()`` user's context (#131)."""

    @pytest.mark.parametrize("measure", _READ_MEASURES)
    def test_sees_the_users_rows(self, sample_orders, scoped_user, guc_mode, enforce_rls, measure):
        """Each method returns what ``list(qs)`` returns: 3 rows for an admin, 2 for tenant A."""
        user, expected = scoped_user
        assert measure(Order.objects.for_user(user)) == expected
        _assert_no_rls_context()

    @pytest.mark.parametrize("check", _PRESENCE_CHECKS)
    def test_finds_a_visible_row(self, sample_orders, scoped_user, guc_mode, enforce_rls, check):
        """Methods answering "is it there?" find a row both users may see."""
        user, _ = scoped_user
        assert check(Order.objects.for_user(user), sample_orders["a1"]) is True
        _assert_no_rls_context()

    def test_tenant_user_without_tenant_raises(self, db, enforce_rls):
        """A non-admin user with ``rls_tenant_id=None`` fails as iteration always did."""
        user = TenantUser.objects.create(username="orphan", tenant=None, is_admin=False)
        with pytest.raises(NoTenantContextError):
            Order.objects.for_user(user).count()
        _assert_no_rls_context()


class TestForUserScopeWrites:
    """Every write method applies the ``for_user()`` user's context (#131)."""

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(lambda qs: qs.update(amount=F("amount")), id="update"),
            pytest.param(
                lambda qs: async_to_sync(qs.aupdate)(amount=F("amount")),
                id="aupdate",
            ),
        ],
    )
    def test_update_reaches_the_users_rows(
        self, sample_orders, scoped_user, guc_mode, enforce_rls, run
    ):
        """``update()`` reports every row the user may see as updated."""
        user, expected = scoped_user
        assert run(Order.objects.for_user(user)) == expected
        _assert_no_rls_context()

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(lambda qs: qs.delete(), id="delete"),
            pytest.param(lambda qs: async_to_sync(qs.adelete)(), id="adelete"),
        ],
    )
    def test_delete_runs_cascades_in_the_scope(
        self, sample_order_items, sample_order_notes, scoped_user, guc_mode, enforce_rls, run
    ):
        """The rows, their CASCADE children and the SET_NULL updates are all reached."""
        user, expected = scoped_user
        _, per_model = run(Order.objects.for_user(user))
        assert per_model["test_app.Order"] == expected
        assert per_model["test_app.OrderItem"] == expected  # one item per order
        _assert_no_rls_context()
        with admin_context():
            assert Order.objects.count() == 3 - expected
            linked = OrderNote.objects.get(pk=sample_order_notes["a_linked"].pk)
            assert linked.order_id is None

    @pytest.mark.parametrize("run", _INSERTS)
    def test_insert_passes_the_policy_check(
        self, tenant_a, scoped_user, guc_mode, enforce_rls, run
    ):
        """The ``INSERT`` satisfies the policy's ``WITH CHECK`` for both users."""
        user, _ = scoped_user
        obj = run(Order.objects.for_user(user), product="New", amount=Decimal(1), tenant=tenant_a)
        assert obj.pk is not None
        _assert_no_rls_context()
        with admin_context():
            assert Order.objects.get(pk=obj.pk).tenant_id == tenant_a.pk

    def test_insert_into_another_tenant_is_rejected(self, tenant_b, tenant_a_user, enforce_rls):
        """The scope sets the user's own tenant, so RLS still rejects a foreign row."""
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            Order.objects.for_user(tenant_a_user).create(
                product="Foreign", amount=Decimal(1), tenant=tenant_b
            )
        _assert_no_rls_context()

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(lambda qs, objs: qs.bulk_update(objs, ["amount"]), id="bulk_update"),
            pytest.param(
                lambda qs, objs: async_to_sync(qs.abulk_update)(objs, ["amount"]),
                id="abulk_update",
            ),
        ],
    )
    def test_bulk_update_reaches_only_the_users_rows(
        self, sample_orders, scoped_user, guc_mode, enforce_rls, run
    ):
        """All three orders are passed in; only those the user may see change."""
        user, expected = scoped_user
        with admin_context():
            orders = list(Order.objects.all())
        for order in orders:
            order.amount = Decimal(99)
        assert run(Order.objects.for_user(user), orders) == expected
        _assert_no_rls_context()
        with admin_context():
            assert Order.objects.filter(amount=Decimal(99)).count() == expected

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(lambda qs, **kw: qs.update_or_create(**kw), id="update_or_create"),
            pytest.param(
                lambda qs, **kw: async_to_sync(qs.aupdate_or_create)(**kw),
                id="aupdate_or_create",
            ),
        ],
    )
    def test_update_or_create_saves_an_existing_row(
        self, sample_orders, scoped_user, guc_mode, enforce_rls, run
    ):
        """``Model.save()`` writes through the base manager, inside the same scope."""
        user, _ = scoped_user
        pk = sample_orders["a1"].pk
        _, created = run(Order.objects.for_user(user), pk=pk, defaults={"amount": Decimal(77)})
        assert created is False
        _assert_no_rls_context()
        with admin_context():
            assert Order.objects.get(pk=pk).amount == Decimal(77)


class TestForUserScopeState:
    """What the scope leaves on the connection and in the tenant state (#131)."""

    @pytest.mark.parametrize("method", ["count", "exists", "aggregate", "update"])
    def test_users_gucs_replace_the_enclosing_context_while_the_query_runs(
        self, tenant_a_user, monkeypatch, method
    ):
        """Inside ``admin_context()`` a tenant user's query runs with the tenant's GUCs.

        The ORM filter of a tenant user's queryset would hide an admin bypass
        left in place, so the GUCs are read while the query runs.
        """
        seen = {}

        def spy(self_inner, *args, **kwargs):
            seen["is_admin"] = get_guc("rls.is_admin")
            seen["tenant"] = get_guc("rls.current_tenant")

        monkeypatch.setattr(QuerySet, method, spy)
        with admin_context():
            getattr(Order.objects.for_user(tenant_a_user), method)()
            assert get_guc("rls.is_admin") == "true"
        assert seen == {"is_admin": "false", "tenant": str(tenant_a_user.rls_tenant_id)}

    @pytest.mark.parametrize(
        "measure",
        [
            pytest.param(lambda qs: len(list(qs)), id="iteration"),
            pytest.param(lambda qs: qs.count(), id="count"),
            pytest.param(lambda qs: len(list(qs.iterator())), id="iterator"),
            pytest.param(lambda qs: async_to_sync(qs.acount)(), id="acount"),
            pytest.param(lambda qs: qs.update(amount=F("amount")), id="update"),
        ],
    )
    def test_enclosing_admin_context_is_restored(
        self, sample_orders, tenant_a_user, guc_mode, enforce_rls, measure
    ):
        """A tenant user's query inside ``admin_context()`` hands the admin GUCs back."""
        with admin_context():
            assert measure(Order.objects.for_user(tenant_a_user)) == 2
            assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.current_tenant") is None
            assert get_current_tenant_id() is None
            assert get_rls_context_active() is True
            assert Order.objects.count() == 3
        _assert_no_rls_context()

    def test_enclosing_tenant_context_is_restored(
        self, sample_orders, admin_user, tenant_b, guc_mode, enforce_rls
    ):
        """An admin user's query inside ``tenant_context()`` leaves no admin bypass behind."""
        with tenant_context(tenant_b.pk):
            # The enclosing context's auto-scope filter still applies.
            assert len(list(Order.objects.for_user(admin_user))) == 1
            assert Order.objects.for_user(admin_user).count() == 1
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_b.pk)
            assert get_current_tenant_id() == tenant_b.pk
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM test_order")
                assert cursor.fetchone()[0] == 1
        _assert_no_rls_context()

    def test_same_users_context_is_left_untouched(
        self, sample_orders, tenant_a, tenant_a_user, enforce_rls, monkeypatch
    ):
        """Inside the user's own context (the middleware case) no GUC is written.

        Before #131, evaluating the queryset cleared the request's GUCs, and
        every later query of the request returned nothing.
        """
        writes = []
        monkeypatch.setattr(managers, "set_guc", lambda *args, **kwargs: writes.append(args))
        with tenant_context(tenant_a.pk):
            assert len(list(Order.objects.for_user(tenant_a_user))) == 2
            assert Order.objects.for_user(tenant_a_user).count() == 2
            assert Order.objects.count() == 2
        assert writes == []
        _assert_no_rls_context()

    def test_plain_queryset_adds_no_queries(self, sample_orders, tenant_a):
        """Without ``for_user()`` the scope is a no-op: one query, nothing else."""
        with tenant_context(tenant_a.pk), CaptureQueriesContext(connection) as queries:
            assert Order.objects.count() == 2
        assert len(queries.captured_queries) == 1

    def test_cached_queryset_runs_no_sql(self, sample_orders, tenant_a_user, enforce_rls):
        """An evaluated queryset answers from its cache without touching the GUCs."""
        qs = Order.objects.for_user(tenant_a_user).order_by("pk")
        assert len(list(qs)) == 2
        with CaptureQueriesContext(connection) as queries:
            assert len(qs) == 2
            assert qs.count() == 2
            assert qs.exists() is True
            assert bool(qs) is True
            assert qs.first() == sample_orders["a1"]
        assert queries.captured_queries == []

    def test_exception_restores_the_state(self, tenant_a_user, monkeypatch):
        """The context is active while the query runs and fully undone when it raises."""
        seen = {}

        def exploding_count(self_inner):
            seen["guc"] = get_guc("rls.current_tenant")
            seen["tenant"] = get_current_tenant_id()
            seen["active"] = get_rls_context_active()
            raise RuntimeError("boom")

        monkeypatch.setattr(QuerySet, "count", exploding_count)
        with pytest.raises(RuntimeError, match="boom"):
            Order.objects.for_user(tenant_a_user).count()
        tenant_id = tenant_a_user.rls_tenant_id
        assert seen == {"guc": str(tenant_id), "tenant": tenant_id, "active": True}
        _assert_no_rls_context()

    def test_exception_right_after_a_write_still_restores(self, admin_user, monkeypatch):
        """A GUC the server has set is restored even if the call then raises."""
        real_set_guc = managers.set_guc

        def set_then_fail(*args, **kwargs):
            real_set_guc(*args, **kwargs)
            raise RuntimeError("interrupted")

        monkeypatch.setattr(managers, "set_guc", set_then_fail)
        with pytest.raises(RuntimeError, match="interrupted"):
            Order.objects.for_user(admin_user).count()
        _assert_no_rls_context()

    def test_aborted_transaction_keeps_the_original_error(
        self, sample_orders, tenant_a_user, enforce_rls
    ):
        """A failing statement's own error propagates, not "current transaction is aborted".

        The restore cannot run in the aborted transaction; the rollback undoes
        the GUC change instead.
        """
        with pytest.raises(DataError, match="division by zero"), transaction.atomic():
            Order.objects.for_user(tenant_a_user).update(amount=F("amount") / 0)
        _assert_no_rls_context()

    def test_restore_survives_a_caught_signal_error(self, sample_orders, admin_user, enforce_rls):
        """A receiver error leaves Django's rollback flag set; the restore still runs.

        ``delete()`` sends its signals inside ``atomic(savepoint=False)``, so the
        error marks the enclosing block for rollback. A caller that catches it
        and clears the flag commits the transaction, so the admin GUCs must
        not still be set.
        """

        def fail(sender, instance, **kwargs):
            raise ValueError("receiver failed")

        pre_delete.connect(fail, sender=Order)
        try:
            with transaction.atomic():
                with pytest.raises(ValueError, match="receiver failed"):
                    Order.objects.for_user(admin_user).delete()
                assert transaction.get_rollback() is True
                transaction.set_rollback(False)
                assert get_guc("rls.is_admin") is None
                with connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM test_order")
                    assert cursor.fetchone()[0] == 0
        finally:
            pre_delete.disconnect(fail, sender=Order)

    def test_local_set_mode_does_not_outlive_the_query(
        self, sample_orders, tenant_a_user, admin_user, tenant_b, settings, enforce_rls
    ):
        """With ``SET LOCAL`` the GUCs no longer stay set for the rest of the transaction."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        assert Order.objects.for_user(admin_user).count() == 3
        assert get_guc("rls.is_admin") is None
        with tenant_context(tenant_b.pk):
            assert len(list(Order.objects.for_user(admin_user))) == 1
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_b.pk)
        _assert_no_rls_context()


@pytest.mark.django_db(transaction=True)
class TestForUserOutsideATransaction:
    """No transaction open: a Celery task, a management command, a script (#131)."""

    def test_iterators_work_in_autocommit(self, sample_orders, scoped_user, enforce_rls):
        """Autocommit cursors are ``WITH HOLD``; the first scope covers the whole result."""
        user, expected = scoped_user
        qs = Order.objects.for_user(user)
        assert len(list(qs.iterator(chunk_size=1))) == expected
        assert len(async_to_sync(_collect)(qs.aiterator(chunk_size=1))) == expected
        _assert_no_rls_context()

    def test_local_set_mode_queries_run_in_transactions_of_their_own(
        self, sample_orders, scoped_user, settings, enforce_rls
    ):
        """No transaction to ``SET LOCAL`` in: the guard gives each statement one (#96)."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        user, expected = scoped_user
        qs = Order.objects.for_user(user)
        assert qs.count() == expected
        assert len(list(qs)) == expected
        assert len(list(qs.iterator(chunk_size=1))) == expected
        assert len(async_to_sync(_collect)(qs.aiterator(chunk_size=1))) == expected
        assert session_gucs() == (None, None)
        _assert_no_rls_context()

    def test_local_set_mode_transactional_method_writes_through_the_guard(
        self, sample_orders, tenant_a_user, settings, enforce_rls
    ):
        """``delete()`` gets the scope's transaction; the scope itself writes nothing in it.

        The guard sets the user's values before the first statement and takes
        them off when the scope exits, each in one ``set_config()`` statement.
        """
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        connection.ensure_connection()
        with CaptureQueriesContext(connection) as queries:
            deleted, _ = Order.objects.for_user(tenant_a_user).filter(product="Widget A1").delete()
        logged = [query["sql"] for query in queries]
        writes = [sql for sql in logged if sql.count("set_config(") == 2]
        assert deleted == 1
        assert (logged[0], logged[-1]) == ("BEGIN", "COMMIT")
        assert len(writes) == 2
        assert not [sql for sql in logged if "SET LOCAL" in sql]
        assert session_gucs() == (None, None)

    def test_local_set_mode_scope_writes_nothing_itself(
        self, sample_orders, tenant_a_user, settings, enforce_rls
    ):
        """No snapshot, no write, no restore: the guard's own transaction around the query."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        connection.ensure_connection()
        with CaptureQueriesContext(connection) as queries:
            assert Order.objects.for_user(tenant_a_user).count() == 2
        logged = [query["sql"] for query in queries]
        assert len(logged) == 4
        assert logged[0] == "BEGIN"
        assert logged[1].count("set_config(") == 2
        assert "COMMIT" in logged

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(
                lambda qs, orders: (
                    qs.update_or_create(
                        pk=orders["a1"].pk,
                        defaults={"amount": Decimal(5)},
                    )[1]
                    is False
                ),
                id="update_or_create",
            ),
            pytest.param(
                lambda qs, orders: qs.bulk_update([orders["a1"]], ["amount"]) == 1,
                id="bulk_update",
            ),
            pytest.param(
                lambda qs, orders: len(qs.bulk_create([_new_order(orders["a1"].tenant_id)])) == 1,
                id="bulk_create",
            ),
            pytest.param(
                lambda qs, orders: qs.filter(pk=orders["a1"].pk).delete()[0] == 1,
                id="delete",
            ),
        ],
    )
    def test_local_set_mode_transactional_methods_open_one(
        self, sample_orders, tenant_a_user, settings, enforce_rls, run
    ):
        """Methods Django runs in a transaction of their own get it before their ``SET LOCAL``.

        ``update_or_create()`` worked here before #131: its lookup set the
        GUCs inside Django's transaction, and they lasted for the save.
        """
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        assert run(Order.objects.for_user(tenant_a_user), sample_orders) is True
        _assert_no_rls_context()


class TestForUserIterators:
    """``iterator()`` and ``aiterator()`` apply the scope per chunk (#131)."""

    def test_scope_is_not_held_in_the_loop_body(
        self, sample_orders, scoped_user, guc_mode, enforce_rls
    ):
        """The caller's code between chunks runs without the user's context."""
        user, expected = scoped_user
        states = [
            (
                get_guc("rls.is_admin"),
                get_guc("rls.current_tenant"),
                get_current_tenant_id(),
                get_rls_context_active(),
            )
            for _ in Order.objects.for_user(user).iterator(chunk_size=1)
        ]
        assert len(states) == expected
        assert set(states) == {(None, None, None, False)}

    def test_every_fetch_runs_in_the_scope(
        self, sample_orders, scoped_user, guc_mode, enforce_rls
    ):
        """A ``UNION ALL`` branch first reached by a later fetch still sees the user's GUCs.

        In the test transaction the cursor produces rows at fetch time, and
        PostgreSQL evaluates the policy of the second branch only when a fetch
        reaches it. The ``FETCH`` bypasses the GUC affinity guard, so with
        ``USE_LOCAL_SET=True`` the chunk's scope applies the values itself
        (#137): the previous chunk's exit took them off the connection.
        """
        user, _ = scoped_user
        qs = Order.objects.for_user(user)
        both = qs.filter(product="Widget A1").union(qs.filter(product="Widget A2"), all=True)
        assert sorted(o.product for o in both.iterator(chunk_size=1)) == ["Widget A1", "Widget A2"]

    def test_later_fetches_do_not_run_under_the_enclosing_context(
        self, sample_orders, tenant_a_user, guc_mode, enforce_rls
    ):
        """A chunk fetched after the enclosing admin context came back sees the user's rows only.

        Between chunks the scope is closed and the enclosing context's values
        are back on the connection; the next chunk's ``FETCH`` must not run
        under them (#137).
        """
        qs = Order.objects.for_user(tenant_a_user)
        both = qs.filter(product="Widget A1").union(qs.filter(product="Gadget B1"), all=True)
        with admin_context():
            products = [o.product for o in both.iterator(chunk_size=1)]
        assert products == ["Widget A1"]

    def test_chunk_scopes_add_one_statement_each_in_local_mode(
        self, sample_orders, tenant_a_user, enforce_rls, settings
    ):
        """In a transaction each chunk costs the scope's write and its exit's clear, no more.

        The three ``FETCH``es (the last one empty) run past Django's cursor and
        are not logged; the cursor's ``DECLARE`` is.
        """
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        Order.objects.for_user(tenant_a_user).count()  # settles the connection
        with CaptureQueriesContext(connection) as queries:
            assert len(list(Order.objects.for_user(tenant_a_user).iterator(chunk_size=1))) == 2

        def kind(sql):
            if sql.startswith("DECLARE"):
                return "DECLARE"
            assert sql.count("set_config(") == 2, sql
            return "SET" if ", true)" in sql else "CLEAR"

        assert [kind(q["sql"]) for q in queries] == [
            "SET",  # the first chunk's scope, before the DECLARE
            "DECLARE",
            "CLEAR",  # its exit: no enclosing context
            *["SET", "CLEAR"] * 2,  # the second chunk, then the empty one
        ]

    def test_breaking_out_leaves_no_context(self, sample_orders, tenant_a_user, enforce_rls):
        """Stopping early leaves nothing set: no scope was open between chunks."""
        for _ in Order.objects.for_user(tenant_a_user).iterator(chunk_size=1):
            break
        _assert_no_rls_context()

    def test_iterator_validates_chunk_size_eagerly(self, tenant_a_user):
        """Like Django, a bad ``chunk_size`` fails at the call, before any query."""
        with pytest.raises(ValueError, match="strictly positive"):
            Order.objects.for_user(tenant_a_user).iterator(chunk_size=0)

    @pytest.mark.parametrize(
        ("chunk_size", "error"),
        [(0, ValueError), (-1, ValueError), (None, TypeError)],
    )
    def test_aiterator_validates_chunk_size(self, tenant_a_user, chunk_size, error):
        """``aiterator()`` keeps Django's ``chunk_size`` check, before any query."""
        with pytest.raises(error):
            async_to_sync(_collect)(
                Order.objects.for_user(tenant_a_user).aiterator(chunk_size=chunk_size),
            )

    def test_default_chunk_size_matches_django(self):
        """The scope's chunk follows Django's own default, so one scope covers one fetch."""
        assert f"chunk_size or {_ITERATOR_CHUNK_SIZE}" in inspect.getsource(QuerySet._iterator)
        default = inspect.signature(QuerySet.aiterator).parameters["chunk_size"].default
        assert default == _ITERATOR_CHUNK_SIZE

    @pytest.mark.usefixtures("_strict_mode")
    @pytest.mark.parametrize(
        "collect",
        [
            pytest.param(lambda qs: list(qs.iterator(chunk_size=1)), id="iterator"),
            pytest.param(
                lambda qs: async_to_sync(_collect)(qs.aiterator(chunk_size=1)),
                id="aiterator",
            ),
        ],
    )
    def test_prefetch_related_runs_in_the_scope(
        self, sample_order_items, tenant_a_user, enforce_rls, collect
    ):
        """Each chunk's prefetch query is scoped and passes ``STRICT_MODE``."""
        qs = Order.objects.for_user(tenant_a_user).prefetch_related("orderitem_set")
        orders = collect(qs)
        items = sorted(item.description for order in orders for item in order.orderitem_set.all())
        assert items == ["Part A1-1", "Part A2-1"]
        _assert_no_rls_context()

    def test_aiterator_without_for_user_uses_the_enclosing_context(
        self, sample_orders, tenant_a, enforce_rls
    ):
        """Without ``for_user()``, ``aiterator()`` is Django's, under the caller's context."""

        async def products():
            async with async_tenant_context(tenant_a.pk):
                return [order.product async for order in Order.objects.aiterator()]

        assert sorted(async_to_sync(products)()) == ["Widget A1", "Widget A2"]

    @pytest.mark.usefixtures("_strict_mode")
    def test_aiterator_without_context_raises_in_strict_mode(self, sample_orders):
        """``aiterator()`` is guarded like ``iterator()``."""
        with pytest.raises(NoTenantContextError):
            async_to_sync(_collect)(Order.objects.aiterator())


class TestForUserStrictMode:
    """``STRICT_MODE`` with ``for_user()`` and cached querysets (#131)."""

    @pytest.mark.usefixtures("_strict_mode")
    def test_prefetch_related_results_are_readable_later(
        self, sample_order_items, tenant_a_user, enforce_rls
    ):
        """Prefetched querysets are cached: read after the scope, as a template would."""
        orders = list(Order.objects.for_user(tenant_a_user).prefetch_related("orderitem_set"))
        items = sorted(item.description for order in orders for item in order.orderitem_set.all())
        assert items == ["Part A1-1", "Part A2-1"]

    @pytest.mark.usefixtures("_strict_mode")
    def test_cached_queryset_is_readable_outside_its_context(self, sample_orders, tenant_a):
        """Reading a cached queryset runs no query, so the guard has nothing to stop."""
        with tenant_context(tenant_a.pk):
            qs = Order.objects.order_by("pk")
            assert len(qs) == 2
        assert len(qs) == 2
        assert qs.count() == 2
        assert qs.exists() is True
        assert qs.first() == sample_orders["a1"]
        with pytest.raises(NoTenantContextError):
            qs.last()  # queries a reversed clone

    @pytest.mark.usefixtures("_strict_mode")
    def test_fast_delete_is_guarded(self, sample_documents):
        """A model without relations is deleted without ``_fetch_all()``; it is still guarded."""
        with pytest.raises(NoTenantContextError):
            Document.objects.all().delete()

    @pytest.mark.usefixtures("_strict_mode")
    def test_signal_handlers_run_in_the_users_context(
        self, sample_order_items, tenant_a_user, enforce_rls
    ):
        """Code that ``delete()`` calls back into is scoped to the user and passes the guard."""
        seen = []

        def count_items(sender, instance, **kwargs):
            seen.append(OrderItem.objects.count())

        pre_delete.connect(count_items, sender=Order)
        try:
            Order.objects.for_user(tenant_a_user).delete()
        finally:
            pre_delete.disconnect(count_items, sender=Order)
        assert seen == [2, 2]


class TestTenantQuerySetDeleteFlag:
    """``delete()`` keeps the ``queryset_only`` flag of the method it overrides.

    (Django's ``AltersData`` already copies ``alters_data`` onto overrides.)
    """

    def test_managers_built_from_the_queryset_have_no_delete(self):
        """No ``Manager.delete()``, which would empty the table in one call."""
        assert not hasattr(TenantQuerySet.as_manager(), "delete")
        assert not hasattr(RLSManager.from_queryset(TenantQuerySet)(), "delete")


class _ReadsElsewhereRouter:
    """Routes reads to an alias that does not exist and writes to ``default``."""

    def db_for_read(self, model, **hints):
        return "reads_elsewhere"

    def db_for_write(self, model, **hints):
        return "default"


class _AlternatingReadRouter:
    """Answers ``default``, then an alias that does not exist, in turn, for every read."""

    answers = itertools.cycle(["default", "reads_elsewhere"])

    def db_for_read(self, model, **hints):
        return next(self.answers)

    def db_for_write(self, model, **hints):
        return "default"


_READS_ELSEWHERE = f"{__name__}._ReadsElsewhereRouter"
_ALTERNATING_READS = f"{__name__}._AlternatingReadRouter"


class TestForUserAliases:
    """The scope sets the GUCs on the alias the SQL runs on (#131)."""

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(lambda qs, orders: qs.update(amount=F("amount")) == 2, id="update"),
            pytest.param(lambda qs, orders: qs.delete()[0] == 2, id="delete"),
            pytest.param(
                lambda qs, orders: (
                    qs.create(
                        product="New", amount=Decimal(1), tenant_id=orders["a1"].tenant_id
                    ).pk
                    is not None
                ),
                id="create",
            ),
            pytest.param(
                lambda qs, orders: len(qs.bulk_create([_new_order(orders["a1"].tenant_id)])) == 1,
                id="bulk_create",
            ),
            pytest.param(
                lambda qs, orders: qs.bulk_update([orders["a1"]], ["amount"]) == 1,
                id="bulk_update",
            ),
            pytest.param(
                lambda qs, orders: (
                    qs.update_or_create(pk=orders["a1"].pk, defaults={"amount": Decimal(5)})[1]
                    is False
                ),
                id="update_or_create",
            ),
        ],
    )
    def test_writes_use_the_write_alias(self, sample_orders, tenant_a_user, enforce_rls, run):
        """A read alias that does not exist proves no write touches it."""
        with override_settings(DATABASE_ROUTERS=[_READS_ELSEWHERE]):
            assert run(Order.objects.for_user(tenant_a_user), sample_orders) is True
        _assert_no_rls_context()

    def test_write_alias_follows_the_router(self, db):
        """``_db_for_write()`` is the router's write alias; ``db`` stays the read alias."""
        with override_settings(DATABASE_ROUTERS=[_READS_ELSEWHERE]):
            qs = Order.objects.all()
            assert qs.db == "reads_elsewhere"
            assert qs._db_for_write() == "default"
            assert qs.using("default")._db_for_write() == "default"

    def test_reads_use_the_querysets_own_alias(self, tenant_a_user):
        """``.using()`` names the alias the scope works on."""
        with pytest.raises(ConnectionDoesNotExist):
            Order.objects.for_user(tenant_a_user).using("nowhere").count()

    @pytest.mark.parametrize(
        ("method", "alias"),
        [("count", "reads_elsewhere"), ("update", "default")],
    )
    def test_every_statement_of_the_scope_uses_one_alias(
        self, tenant_a_user, monkeypatch, method, alias
    ):
        """Snapshot, writes, restore and the query itself all run on the same alias."""
        used = []

        def snapshot(conf, *, using):
            used.append(("snapshot", using))
            return {conf.GUC_IS_ADMIN: None, conf.GUC_CURRENT_TENANT: None}

        def query(self_inner, *args, **kwargs):
            used.append(("query", self_inner.db))

        @contextmanager
        def writing(using, values, *, local=False):
            used.append(("writing", using))
            yield

        monkeypatch.setattr(managers, "_snapshot_gucs", snapshot)
        monkeypatch.setattr(managers, "_session_of", lambda using: used.append(("session", using)))
        monkeypatch.setattr(managers, "writing", writing)
        monkeypatch.setattr(
            managers, "set_guc", lambda name, value, **kw: used.append(("set", kw["using"]))
        )
        monkeypatch.setattr(
            managers,
            "_restore_tenant_gucs",
            lambda changed, **kw: used.append(("restore", kw["using"])),
        )
        monkeypatch.setattr(QuerySet, method, query)
        with override_settings(DATABASE_ROUTERS=[_READS_ELSEWHERE]):
            getattr(Order.objects.for_user(tenant_a_user), method)()
        assert used == [
            ("snapshot", alias),
            ("session", alias),
            ("writing", alias),
            ("set", alias),
            ("set", alias),
            ("query", alias),
            ("restore", alias),
        ]

    @pytest.mark.parametrize(
        "check",
        [
            pytest.param(lambda qs: len(list(qs)) == 2, id="iteration"),
            pytest.param(lambda qs: qs.count() == 2, id="count"),
            pytest.param(lambda qs: qs.exists(), id="exists"),
            pytest.param(lambda qs: qs.aggregate(n=Count("id"))["n"] == 2, id="aggregate"),
            pytest.param(lambda qs: len(list(qs.iterator(chunk_size=1))) == 2, id="iterator"),
        ],
    )
    def test_alias_is_resolved_once_per_call(
        self, sample_orders, tenant_a_user, enforce_rls, monkeypatch, check
    ):
        """A router that answers differently each time cannot split the GUCs from the query."""
        monkeypatch.setattr(
            _AlternatingReadRouter, "answers", itertools.cycle(["default", "reads_elsewhere"])
        )
        with override_settings(DATABASE_ROUTERS=[_ALTERNATING_READS]):
            assert check(Order.objects.for_user(tenant_a_user)) is True
        _assert_no_rls_context()


class TestManagerHints:
    """``RLSManager.get_queryset()`` keeps the manager's router hints, as Django's does."""

    def test_db_manager_hints_reach_the_queryset(self, sample_orders):
        order = Order.objects.first()
        qs = Order.objects.db_manager(hints={"instance": order}).all()
        assert qs._hints == {"instance": order}
        assert qs._rls_user is None

    def test_for_user_keeps_the_hints(self, sample_orders, tenant_a_user):
        order = Order.objects.first()
        qs = Order.objects.db_manager(hints={"instance": order}).for_user(tenant_a_user)
        assert qs._hints == {"instance": order}
