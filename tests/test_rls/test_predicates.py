"""Tests for django_rls_tenants.rls.predicates (#59)."""

from __future__ import annotations

import pytest
from django.db import models
from django.db.migrations.writer import MigrationWriter
from django.test.utils import isolate_apps

from django_rls_tenants.rls.predicates import (
    Predicate,
    RawPredicate,
    SharedWith,
    TenantField,
    _predicates_sql,
    _quoted,
    _validated_predicates,
)
from tests.test_app.models import Order, SharedNote

_NOTE_SHARE = {"through": "test_app.NoteShare", "fk": "note", "tenant_fk": "shared_with"}


class TestSharedWith:
    """``SharedWith`` builds an ``EXISTS`` on the through model's table."""

    def test_sql(self):
        sql = SharedWith(**_NOTE_SHARE).policy_sql(SharedNote, tenant_value="<tenant>")
        assert sql == (
            'EXISTS (SELECT 1 FROM "test_note_share" AS "rls_shared" '
            'WHERE "rls_shared"."note_id" = "test_shared_note"."id" '
            'AND "rls_shared"."shared_with_id" = <tenant>)'
        )

    def test_fk_to_a_proxy_of_the_model_is_accepted(self):
        """``Assignment.order`` points at ``OrderProxy``, whose table is ``Order``'s."""
        predicate = SharedWith(through="test_app.Assignment", fk="order", tenant_fk="tenant")
        sql = predicate.policy_sql(Order, tenant_value="<tenant>")
        assert '"rls_shared"."order_id" = "test_order"."id"' in sql
        assert '"rls_shared"."tenant_id" = <tenant>' in sql

    def test_unknown_through_model_names_the_dependency(self):
        predicate = SharedWith(through="test_app.Missing", fk="note", tenant_fk="shared_with")
        with pytest.raises(
            ValueError, match=r"there is no model 'test_app\.Missing'.*dependencies"
        ):
            predicate.policy_sql(SharedNote, tenant_value="x")

    def test_fk_to_another_model_is_refused(self):
        predicate = SharedWith(through="test_app.NoteShare", fk="shared_with", tenant_fk="tenant")
        with pytest.raises(ValueError, match=r"to test_app\.Tenant, not to test_app\.SharedNote"):
            predicate.policy_sql(SharedNote, tenant_value="x")

    @pytest.mark.parametrize(
        ("fk", "tenant_fk", "match"),
        [
            ("nope", "shared_with", r"fk='nope': test_app.NoteShare has no field 'nope'"),
            ("id", "shared_with", r"fk='id': test_app.NoteShare.id is a BigAutoField, not"),
            ("note", "nope", r"tenant_fk='nope': test_app.NoteShare has no field 'nope'"),
        ],
    )
    def test_missing_or_non_foreign_key_fields_are_refused(self, fk, tenant_fk, match):
        predicate = SharedWith(through="test_app.NoteShare", fk=fk, tenant_fk=tenant_fk)
        with pytest.raises(ValueError, match=match):
            predicate.policy_sql(SharedNote, tenant_value="x")

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({**_NOTE_SHARE, "through": "NoteShare"}, "Invalid model path for through"),
            ({**_NOTE_SHARE, "through": 'a."b'}, "Invalid model path for through"),
            ({**_NOTE_SHARE, "fk": "note_id; DROP"}, "Invalid field name for fk"),
            ({**_NOTE_SHARE, "tenant_fk": "a.b"}, "Invalid field name for tenant_fk"),
        ],
    )
    def test_invalid_arguments_are_refused(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            SharedWith(**kwargs)

    def test_arguments_are_keyword_only(self):
        with pytest.raises(TypeError):
            SharedWith("test_app.NoteShare", "note", "shared_with")  # type: ignore[misc]


class TestTenantField:
    """``TenantField`` compares a foreign key's column with the current tenant."""

    def test_sql(self):
        sql = TenantField("partner").policy_sql(SharedNote, tenant_value="<tenant>")
        assert sql == '"test_shared_note"."partner_id" = <tenant>'

    def test_missing_field_is_refused(self):
        with pytest.raises(ValueError, match=r"field='nope': test_app\.SharedNote has no field"):
            TenantField("nope").policy_sql(SharedNote, tenant_value="x")

    def test_field_that_is_not_a_foreign_key_is_refused(self):
        with pytest.raises(
            ValueError, match=r"SharedNote\.title is a CharField, not a ForeignKey"
        ):
            TenantField("title").policy_sql(SharedNote, tenant_value="x")

    def test_invalid_name_is_refused(self):
        with pytest.raises(ValueError, match="Invalid field name for field"):
            TenantField('partner"')

    @isolate_apps("tests.test_app")
    def test_column_that_cannot_be_interpolated_is_refused(self):
        class Quoted(models.Model):  # noqa: DJ008  -- throwaway model, never printed
            partner = models.ForeignKey(
                "test_app.Tenant", on_delete=models.CASCADE, db_column='partner"id'
            )

            class Meta:
                app_label = "test_app"

        with pytest.raises(ValueError, match="column name 'partner\"id' contains a quote"):
            TenantField("partner").policy_sql(Quoted, tenant_value="x")

    @isolate_apps("tests.test_app")
    def test_table_that_cannot_be_interpolated_is_refused(self):
        class Dollar(models.Model):  # noqa: DJ008  -- throwaway model, never printed
            partner = models.ForeignKey("test_app.Tenant", on_delete=models.CASCADE)

            class Meta:
                app_label = "test_app"
                db_table = "a$b"

        with pytest.raises(ValueError, match="table name 'a\\$b' contains a quote"):
            TenantField("partner").policy_sql(Dollar, tenant_value="x")


class TestRawPredicate:
    """``RawPredicate`` is used as written, in parentheses."""

    def test_sql(self):
        predicate = RawPredicate("is_public AND partner_id = 3")
        assert predicate.policy_sql(SharedNote, tenant_value="x") == (
            "(is_public AND partner_id = 3)"
        )

    def test_percent_is_doubled_for_the_schema_editor(self):
        """Django composes DDL with its (empty) parameters: a lone ``%`` fails ``migrate``."""
        sql = RawPredicate("title LIKE 'a%' AND title NOT LIKE '%z'").policy_sql(
            SharedNote, tenant_value="NULL"
        )
        assert sql == "(title LIKE 'a%%' AND title NOT LIKE '%%z')"
        assert sql % () == "(title LIKE 'a%' AND title NOT LIKE '%z')"

    def test_string_literals_are_allowed(self):
        predicate = RawPredicate("status = 'shared'")
        assert predicate.policy_sql(SharedNote, tenant_value="x") == "(status = 'shared')"

    @pytest.mark.parametrize("token", [";", "--", "/*", "$", "\x00"])
    def test_text_that_breaks_out_of_the_policy_is_refused(self, token):
        with pytest.raises(ValueError, match="RawPredicate: the SQL expression"):
            RawPredicate(f"true {token} false")

    @pytest.mark.parametrize("sql", ["", "   "])
    def test_empty_expression_is_refused(self, sql):
        with pytest.raises(ValueError, match="the SQL expression is empty"):
            RawPredicate(sql)


class TestEqualityAndRepr:
    """Predicates compare by class and arguments: ``RLSConstraint.__eq__`` relies on it."""

    def test_equal_arguments_are_equal(self):
        assert SharedWith(**_NOTE_SHARE) == SharedWith(**_NOTE_SHARE)
        assert hash(SharedWith(**_NOTE_SHARE)) == hash(SharedWith(**_NOTE_SHARE))
        assert TenantField("partner") == TenantField("partner")
        assert RawPredicate("true") == RawPredicate("true")

    def test_different_arguments_are_not_equal(self):
        assert SharedWith(**_NOTE_SHARE) != SharedWith(**{**_NOTE_SHARE, "fk": "other"})
        assert TenantField("partner") != TenantField("owner")

    def test_different_classes_are_not_equal(self):
        assert TenantField("partner") != RawPredicate("partner")
        assert TenantField("partner") != "partner"

    def test_repr(self):
        assert repr(SharedWith(**_NOTE_SHARE)) == (
            "SharedWith(through='test_app.NoteShare', fk='note', tenant_fk='shared_with')"
        )
        assert repr(TenantField("partner")) == "TenantField('partner')"
        assert repr(RawPredicate("true")) == "RawPredicate('true')"


class TestDeconstruct:
    """A migration writes the arguments a predicate was created with."""

    def test_shared_with(self):
        assert SharedWith(**_NOTE_SHARE).deconstruct() == (
            "django_rls_tenants.rls.predicates.SharedWith",
            (),
            _NOTE_SHARE,
        )

    def test_tenant_field_and_raw_predicate(self):
        assert TenantField("partner").deconstruct() == (
            "django_rls_tenants.rls.predicates.TenantField",
            ("partner",),
            {},
        )
        assert RawPredicate(sql="true").deconstruct() == (
            "django_rls_tenants.rls.predicates.RawPredicate",
            (),
            {"sql": "true"},
        )

    def test_migration_writer_serializes_and_imports_the_module(self):
        source, imports = MigrationWriter.serialize(SharedWith(**_NOTE_SHARE))
        assert source == (
            "django_rls_tenants.rls.predicates.SharedWith(fk='note', "
            "tenant_fk='shared_with', through='test_app.NoteShare')"
        )
        assert imports == {"import django_rls_tenants.rls.predicates"}


class TestHelpers:
    def test_validated_predicates(self):
        predicates = (TenantField("partner"), RawPredicate("true"))
        assert _validated_predicates(None) == []
        assert _validated_predicates(predicates) == list(predicates)

    def test_an_item_that_is_not_a_predicate_is_refused(self):
        with pytest.raises(TypeError, match=r"takes Predicate instances .* not 'partner'"):
            _validated_predicates([TenantField("partner"), "partner"])

    def test_predicates_sql(self):
        predicates = [TenantField("partner"), RawPredicate("true")]
        assert _predicates_sql(SharedNote, predicates, tenant_value="t") == [
            '"test_shared_note"."partner_id" = t',
            "(true)",
        ]

    def test_base_class_builds_nothing(self):
        with pytest.raises(NotImplementedError):
            Predicate().policy_sql(SharedNote, tenant_value="x")

    def test_quoted(self):
        assert _quoted("note_id", "column name") == '"note_id"'
        for name in ['a"b', "a'b", "a\\b", "a$b", "a\x00b"]:
            with pytest.raises(ValueError, match="cannot hold"):
                _quoted(name, "column name")
