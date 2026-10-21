"""Tests for django_rls_tenants.tenants.models."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, connections, models
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.managers import RLSManager
from django_rls_tenants.tenants.models import (
    RLSProtectedModel,
    _add_tenant_fk,
    _declares_rls_policy,
    _get_tenant_fk_field,
    _tenant_field,
    validate_cross_tenant_fks,
)
from tests.test_app.models import (
    Assignment,
    Document,
    Label,
    Loan,
    NoteRevision,
    Order,
    OrderItem,
    OrderNote,
    OrderProxy,
    OrderShipment,
    ProtectedUser,
    Tag,
    Tenant,
)
from tests.test_external_app.models import Book, BookProxy

pytestmark = pytest.mark.django_db


class TestDynamicTenantFK:
    """Tests for the class_prepared signal that adds tenant FK."""

    def test_order_gets_tenant_fk(self):
        """Order (standard RLSProtectedModel) has an auto-generated tenant FK."""
        field_names = [f.name for f in Order._meta.local_fields]
        assert "tenant" in field_names

    def test_document_gets_tenant_fk(self):
        """Document (standard RLSProtectedModel) has an auto-generated tenant FK."""
        field_names = [f.name for f in Document._meta.local_fields]
        assert "tenant" in field_names

    def test_tenant_fk_target(self):
        """Auto-generated FK points to the configured TENANT_MODEL."""
        tenant_field = Order._meta.get_field("tenant")
        assert tenant_field.related_model is Tenant

    def test_tenant_fk_not_nullable(self):
        """Auto-generated FK is NOT NULL (required)."""
        tenant_field = Order._meta.get_field("tenant")
        assert tenant_field.null is False

    def test_explicit_fk_not_overridden(self):
        """ProtectedUser defines its own tenant FK -- signal skips it.

        The explicit FK is nullable (unlike the auto-generated one).
        """
        tenant_field = ProtectedUser._meta.get_field("tenant")
        assert tenant_field.null is True
        assert tenant_field.blank is True

    def test_non_rls_model_unaffected(self):
        """Non-RLSProtectedModel models are not modified by the signal."""
        field_names = [f.name for f in Tenant._meta.local_fields]
        # Tenant has "id" and "name", no "tenant" FK
        assert "tenant" not in field_names


class TestDeclaresRLSPolicy:
    """``_declares_rls_policy()``: the models whose M2M tables the library covers (#159)."""

    def test_rls_protected_model_subclass(self):
        assert _declares_rls_policy(Order)

    def test_model_with_an_rls_constraint_and_no_base(self):
        assert not issubclass(Label, RLSProtectedModel)
        assert _declares_rls_policy(Label)

    def test_proxy_reads_its_concrete_model(self):
        assert _declares_rls_policy(OrderProxy)

    def test_plain_model(self):
        assert not _declares_rls_policy(Tag)

    def test_external_model_does_not_declare_its_policy(self):
        """An ``EXTERNAL_MODELS`` entry is protected, but its policy is the project's migration."""
        assert _get_tenant_fk_field(Book) == "owner"
        assert not _declares_rls_policy(Book)
        assert not _declares_rls_policy(BookProxy)


class TestProxyModel:
    """A proxy of an ``RLSProtectedModel`` gets no tenant FK of its own."""

    def test_proxy_shares_the_concrete_tenant_fk(self):
        assert OrderProxy._meta.local_fields == []
        assert OrderProxy._meta.get_field("tenant") is Order._meta.get_field("tenant")

    def test_proxy_rows_are_the_concrete_models(self, tenant_a, sample_orders):
        with tenant_context(tenant_a.pk):
            assert sorted(OrderProxy.objects.values_list("product", flat=True)) == [
                "Widget A1",
                "Widget A2",
            ]


class TestRLSProtectedModelMeta:
    """Tests for RLSProtectedModel Meta and manager."""

    def test_meta_constraints_inherited(self):
        """Subclass inherits RLSConstraint from RLSProtectedModel.Meta."""
        constraints = Order._meta.constraints
        rls_constraints = [c for c in constraints if isinstance(c, RLSConstraint)]
        assert len(rls_constraints) == 1
        assert rls_constraints[0].field == "tenant"

    def test_meta_constraints_overridden(self):
        """ProtectedUser overrides constraints with custom RLSConstraint."""
        constraints = ProtectedUser._meta.constraints
        rls_constraints = [c for c in constraints if isinstance(c, RLSConstraint)]
        assert len(rls_constraints) == 1
        assert rls_constraints[0].extra_bypass_flags == [
            "rls.is_login_request",
            "rls.is_preauth_request",
        ]

    def test_default_manager_is_rls_manager(self):
        """RLSProtectedModel subclasses use RLSManager as the default manager."""
        assert isinstance(Order.objects, RLSManager)

    def test_abstract_model(self):
        """RLSProtectedModel itself is abstract."""
        assert RLSProtectedModel._meta.abstract is True


class TestAddTenantFKConfig:
    """Tests for _add_tenant_fk respecting TENANT_FK_FIELD configuration."""

    def test_respects_custom_tenant_fk_field(self):
        """_add_tenant_fk uses the configured TENANT_FK_FIELD, not hardcoded 'tenant'.

        When TENANT_FK_FIELD is set to a custom name, the signal handler should
        check for that name when deciding whether to add the FK.
        """
        with patch("django_rls_tenants.tenants.conf.rls_tenants_config") as mock_config:
            mock_config.TENANT_FK_FIELD = "organization"
            mock_config.TENANT_MODEL = "test_app.Tenant"

            # Order already has a field named "tenant" but NOT "organization".
            # With the old hardcoded check, it would skip (because "tenant" IS in
            # local_field_names). With the fix, it should try to add "organization".
            # We patch contribute_to_class to verify it's called with the right name.
            with patch.object(models.ForeignKey, "contribute_to_class") as mock_ctc:
                _add_tenant_fk(sender=Order)
                mock_ctc.assert_called_once()
                _, call_field_name = mock_ctc.call_args[0]
                assert call_field_name == "organization"

    def test_no_duplicate_when_model_defines_configured_fk(self):
        """No FK is added when the model already defines a field matching TENANT_FK_FIELD.

        If a user configures TENANT_FK_FIELD='tenant' and their model already
        has a 'tenant' field (like ProtectedUser), the signal must skip it.
        """
        with patch("django_rls_tenants.tenants.conf.rls_tenants_config") as mock_config:
            mock_config.TENANT_FK_FIELD = "tenant"
            mock_config.TENANT_MODEL = "test_app.Tenant"

            with patch.object(models.ForeignKey, "contribute_to_class") as mock_ctc:
                # ProtectedUser already defines "tenant" field
                _add_tenant_fk(sender=ProtectedUser)
                mock_ctc.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-tenant foreign keys (#107)
# ---------------------------------------------------------------------------


def _invalid(model_name, pk):
    """The message Django's foreign key validation gives a target that does not exist."""
    return f"{model_name} instance with id {pk} is not a valid choice."


def _errors(instance, **kwargs):
    """Return ``full_clean()``'s errors (``{}`` when it passes)."""
    try:
        instance.full_clean(**kwargs)
    except ValidationError as error:
        return error.message_dict
    return {}


class OrderItemForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = ("tenant", "order", "description")


class DescriptionOnlyForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = ("description",)


class TestCrossTenantForeignKeys:
    """``validate_cross_tenant_fks()`` and ``RLSProtectedModel.clean_fields()``."""

    def test_same_tenant_target_passes(self, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["a1"], description="x")
        with admin_context():
            assert _errors(item) == {}

    def test_other_tenants_target_is_rejected(self, tenant_a, sample_orders):
        b1 = sample_orders["b1"]
        item = OrderItem(tenant=tenant_a, order=b1, description="x")
        with admin_context(), pytest.raises(ValidationError) as excinfo:
            item.full_clean()
        assert excinfo.value.message_dict == {"order": [_invalid("order", b1.pk)]}
        assert excinfo.value.error_dict["order"][0].code == "invalid"

    def test_error_is_the_one_a_missing_target_gets(self, tenant_a, sample_orders):
        missing = OrderItem(tenant=tenant_a, order_id=999_999, description="x")
        other = OrderItem(tenant=tenant_a, order_id=sample_orders["b1"].pk, description="x")
        with admin_context():
            missing_errors = _errors(missing)
            other_errors = _errors(other)
        assert missing_errors == {"order": [_invalid("order", 999_999)]}
        assert other_errors == {"order": [_invalid("order", sample_orders["b1"].pk)]}

    def test_tenant_context_reports_one_error(self, tenant_a, sample_orders):
        """Whether RLS hid the row (plain role) or this check caught it (superuser)."""
        b1 = sample_orders["b1"]
        item = OrderItem(tenant=tenant_a, order_id=b1.pk, description="x")
        with tenant_context(tenant_a.pk):
            assert _errors(item) == {"order": [_invalid("order", b1.pk)]}

    def test_tenant_context_under_rls_reports_one_error(
        self, enforce_rls, tenant_a, sample_orders
    ):
        b1 = sample_orders["b1"]
        item = OrderItem(tenant=tenant_a, order_id=b1.pk, description="x")
        with tenant_context(tenant_a.pk):
            assert _errors(item) == {"order": [_invalid("order", b1.pk)]}

    def test_compares_with_the_instances_tenant_not_the_context(
        self, enforce_rls, tenant_a, tenant_b, sample_orders
    ):
        item = OrderItem(tenant=tenant_a, order=sample_orders["a1"], description="x")
        with tenant_context(tenant_b.pk):
            validate_cross_tenant_fks(item)
        validate_cross_tenant_fks(item)  # no context at all

    def test_strict_mode_without_a_context(self, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["b1"], description="x")
        strict = override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "STRICT_MODE": True})
        with strict, pytest.raises(ValidationError):
            validate_cross_tenant_fks(item)

    def test_the_context_is_restored(self, enforce_rls, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["b1"], description="x")
        with tenant_context(tenant_a.pk):
            with pytest.raises(ValidationError):
                validate_cross_tenant_fks(item)
            assert set(Order.objects.values_list("product", flat=True)) == {
                "Widget A1",
                "Widget A2",
            }
            assert Order._base_manager.count() == 2  # RLS, not the auto-scope filter

    def test_target_without_a_tenant_is_rejected(self, tenant_a, sample_protected_users):
        with admin_context():
            nobody = ProtectedUser.objects.create(email="root@example.com", tenant=None)
        assignment = Assignment(tenant=tenant_a, assignee=nobody, title="x")
        with pytest.raises(ValidationError) as excinfo:
            validate_cross_tenant_fks(assignment)
        assert excinfo.value.message_dict == {
            "assignee": [_invalid("protected user", nobody.pk)],
        }
        validate_cross_tenant_fks(
            Assignment(tenant=tenant_a, assignee=sample_protected_users["a"], title="x"),
        )

    def test_foreign_key_to_a_proxy_is_checked(self, tenant_a, sample_orders):
        assignment = Assignment(tenant=tenant_a, order_id=sample_orders["b1"].pk, title="x")
        with pytest.raises(ValidationError) as excinfo:
            validate_cross_tenant_fks(assignment)
        assert set(excinfo.value.message_dict) == {"order"}
        validate_cross_tenant_fks(
            Assignment(tenant=tenant_a, order_id=sample_orders["a1"].pk, title="x"),
        )

    def test_one_to_one_field_is_checked(self, tenant_a, sample_orders):
        shipment = OrderShipment(tenant=tenant_a, order=sample_orders["b1"], carrier="x")
        with pytest.raises(ValidationError) as excinfo:
            validate_cross_tenant_fks(shipment)
        assert set(excinfo.value.message_dict) == {"order"}

    def test_every_foreign_key_is_reported(self, tenant_a, sample_order_notes, sample_order_items):
        revision = NoteRevision(
            tenant=tenant_a,
            note=sample_order_notes["b_linked"],
            item=sample_order_items["b1"],
            summary="x",
        )
        with pytest.raises(ValidationError) as excinfo:
            validate_cross_tenant_fks(revision)
        assert set(excinfo.value.message_dict) == {"note", "item"}

    def test_one_query_per_foreign_key(self, tenant_a, sample_order_notes, sample_order_items):
        revision = NoteRevision(
            tenant=tenant_a,
            note=sample_order_notes["a_linked"],
            item=sample_order_items["a1"],
            summary="x",
        )
        with CaptureQueriesContext(connection) as queries:
            validate_cross_tenant_fks(revision)
        selects = [q["sql"] for q in queries if 'FROM "test_' in q["sql"]]
        assert len(selects) == 2
        assert any('FROM "test_order_note"' in sql for sql in selects)
        assert any('FROM "test_order_item"' in sql for sql in selects)

    def test_nothing_to_check_runs_no_query(self, tenant_a, sample_orders):
        unsaved = Order(tenant=tenant_a, product="x", amount=1)
        with CaptureQueriesContext(connection) as queries:
            validate_cross_tenant_fks(OrderNote(tenant=tenant_a, order=None, body="x"))
            validate_cross_tenant_fks(OrderItem(tenant=tenant_a, order=unsaved))
            validate_cross_tenant_fks(OrderItem(order=sample_orders["b1"]))  # no tenant yet
            validate_cross_tenant_fks(Tag(name="x"))  # no RLSConstraint
        assert len(queries) == 0

    def test_exclude_skips_a_foreign_key(self, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["b1"], description="x")
        validate_cross_tenant_fks(item, exclude=["order"])
        with admin_context():
            assert _errors(item, exclude=["order"]) == {}

    def test_excluded_tenant_field_is_still_read(self, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["b1"], description="x")
        with admin_context():
            assert set(_errors(item, exclude=["tenant"])) == {"order"}

    def test_invalid_tenant_skips_the_check(self, sample_orders):
        item = OrderItem(tenant_id=999_999, order=sample_orders["b1"], description="x")
        with admin_context():
            assert set(_errors(item)) == {"tenant"}
        validate_cross_tenant_fks(OrderItem(tenant_id="abc", order=sample_orders["b1"]))

    def test_invalid_foreign_key_value(self, tenant_a):
        with pytest.raises(ValidationError) as excinfo:
            validate_cross_tenant_fks(OrderItem(tenant=tenant_a, order_id="abc"))
        assert set(excinfo.value.message_dict) == {"order"}

    def test_other_field_errors_are_kept(self, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["b1"], description="")
        with admin_context():
            assert set(_errors(item)) == {"order", "description"}

    def test_save_does_not_validate(self, tenant_a, sample_orders):
        with admin_context():
            item = OrderItem.objects.create(
                tenant=tenant_a,
                order=sample_orders["b1"],
                description="x",
            )
        assert item.pk is not None

    def test_model_form_rejects_another_tenants_target(self, tenant_a, sample_orders):
        b1 = sample_orders["b1"]
        data = {"tenant": tenant_a.pk, "order": b1.pk, "description": "x"}
        with admin_context():
            form = OrderItemForm(data=data)
            assert not form.is_valid()
        assert form.errors == {"order": [_invalid("order", b1.pk)]}

    def test_model_form_accepts_the_tenants_target(self, tenant_a, sample_orders):
        data = {"tenant": tenant_a.pk, "order": sample_orders["a1"].pk, "description": "x"}
        with admin_context():
            form = OrderItemForm(data=data)
            assert form.is_valid(), form.errors

    def test_model_form_skips_a_foreign_key_it_does_not_show(self, tenant_a, sample_orders):
        """Not a ValueError: an error for a field the form lacks has nowhere to go."""
        instance = OrderItem(tenant=tenant_a, order=sample_orders["b1"])
        with admin_context():
            form = DescriptionOnlyForm(data={"description": "x"}, instance=instance)
            assert form.is_valid(), form.errors

    @pytest.mark.django_db(transaction=True)
    def test_local_set_mode_on_a_router_alias_outside_databases(
        self, replica, tenant_a, sample_orders
    ):
        """A read replica the router returns needs a transaction for the SET LOCAL writes."""
        a1, b1 = sample_orders["a1"], sample_orders["b1"]
        local_set = override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "USE_LOCAL_SET": True})
        with (
            local_set,
            patch("django_rls_tenants.tenants.models.router.db_for_read", return_value=replica),
            tenant_context(tenant_a.pk),
        ):
            with pytest.raises(ValidationError):
                validate_cross_tenant_fks(OrderItem(tenant=tenant_a, order=b1, description="x"))
            validate_cross_tenant_fks(OrderItem(tenant=tenant_a, order=a1, description="x"))
        assert connections[replica].in_atomic_block is False

    @pytest.mark.django_db(transaction=True)
    def test_local_set_mode(self, enforce_rls, tenant_a, sample_orders):
        item = OrderItem(tenant=tenant_a, order=sample_orders["b1"], description="x")
        local_set = override_settings(RLS_TENANTS={**settings.RLS_TENANTS, "USE_LOCAL_SET": True})
        with local_set, tenant_context(tenant_a.pk):
            with pytest.raises(ValidationError):
                validate_cross_tenant_fks(item)
            assert Order._base_manager.count() == 2
            validate_cross_tenant_fks(OrderItem(tenant=tenant_a, order=sample_orders["a1"]))


class TestExternalModels:
    """``RLS_TENANTS["EXTERNAL_MODELS"]`` names the tenant field of a model without a constraint.

    #112: such a model's policy comes from an ``AddRLSPolicy`` migration.
    """

    def test_tenant_field_name_comes_from_the_entry(self):
        assert _get_tenant_fk_field(Book) == "owner"
        assert _tenant_field(Book) is Book._meta.get_field("owner")

    def test_proxy_follows_its_concrete_model(self):
        """A proxy has no entry of its own; ``_tenant_field()`` reads the concrete model."""
        assert _get_tenant_fk_field(BookProxy) is None
        assert _tenant_field(BookProxy) is Book._meta.get_field("owner")

    def test_entry_naming_a_proxy_is_ignored(self, settings):
        """W016 reports it; the proxy's table is the concrete model's."""
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {"test_external_app.BookProxy": "owner"},
        }
        assert _get_tenant_fk_field(BookProxy) is None
        assert _tenant_field(Book) is None

    def test_label_model_name_is_case_insensitive(self, settings):
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {"test_external_app.BOOK": "owner"},
        }
        assert _get_tenant_fk_field(Book) == "owner"

    def test_constraint_wins_over_an_entry(self, settings):
        """W016 reports the entry; the policy is the constraint's."""
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {"test_app.Order": "other"},
        }
        assert _get_tenant_fk_field(Order) == "tenant"

    def test_unlisted_model_has_no_tenant_field(self, settings):
        settings.RLS_TENANTS = {
            key: value for key, value in settings.RLS_TENANTS.items() if key != "EXTERNAL_MODELS"
        }
        assert _get_tenant_fk_field(Book) is None
        assert _tenant_field(Book) is None

    def test_missing_or_non_fk_field_gives_no_tenant_field(self, settings):
        for field in ("missing", "title"):
            settings.RLS_TENANTS = {
                **settings.RLS_TENANTS,
                "EXTERNAL_MODELS": {"test_external_app.Book": field},
            }
            assert _get_tenant_fk_field(Book) == field
            assert _tenant_field(Book) is None


class TestCrossTenantForeignKeysToExternalModels:
    """A foreign key to a model listed in EXTERNAL_MODELS is validated against the tenant."""

    def test_same_tenant_target_passes(self, tenant_a, sample_books):
        loan = Loan(tenant=tenant_a, book=sample_books["a"], borrower="x")
        with admin_context():
            assert _errors(loan) == {}

    def test_other_tenants_target_is_rejected(self, tenant_a, sample_books):
        book_b = sample_books["b"]
        loan = Loan(tenant=tenant_a, book=book_b, borrower="x")
        with admin_context():
            assert _errors(loan) == {"book": [_invalid("book", book_b.pk)]}

    def test_tenant_context_under_rls_reports_one_error(self, enforce_rls, tenant_a, sample_books):
        book_b = sample_books["b"]
        loan = Loan(tenant=tenant_a, book_id=book_b.pk, borrower="x")
        with tenant_context(tenant_a.pk):
            assert _errors(loan) == {"book": [_invalid("book", book_b.pk)]}

    def test_unlisted_external_model_is_not_checked(self, tenant_a, sample_books, settings):
        settings.RLS_TENANTS = {
            key: value for key, value in settings.RLS_TENANTS.items() if key != "EXTERNAL_MODELS"
        }
        loan = Loan(tenant=tenant_a, book=sample_books["b"], borrower="x")
        with admin_context():
            assert _errors(loan) == {}
