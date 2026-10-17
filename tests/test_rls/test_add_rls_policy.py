"""Tests for the ``AddRLSPolicy`` migration operation (#112)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.apps import apps
from django.db import migrations, models, router
from django.db.migrations.state import ModelState, ProjectState
from django.db.migrations.writer import MigrationWriter

from django_rls_tenants.operations import AddRLSPolicy
from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.rls.predicates import RawPredicate, SharedWith, TenantField
from tests.test_external_app.models import Book


@pytest.fixture
def state():
    """The project state of the installed models."""
    return ProjectState.from_apps(apps)


def _with_model(state, name, fields, **options):
    """Add a model ``name`` to app ``ext`` of ``state``, with the fields given after ``id``."""
    state.add_model(
        ModelState(
            "ext",
            name,
            [("id", models.AutoField(primary_key=True)), *fields],
            options={"db_table": f"ext_{name.lower()}", **options},
        )
    )
    return state


def _editor():
    editor = MagicMock()
    editor.connection.alias = "default"
    return editor


def _forwards_sql(op, state, app_label="test_app"):
    editor = _editor()
    op.database_forwards(app_label, editor, state, state)
    editor.execute.assert_called_once()
    return editor.execute.call_args[0][0]


def _backwards_sql(op, state, app_label="test_app"):
    editor = _editor()
    op.database_backwards(app_label, editor, state, state)
    editor.execute.assert_called_once()
    return editor.execute.call_args[0][0]


def _book(**options):
    return AddRLSPolicy("Book", field="owner", app_label="test_external_app", **options)


def _tenant_fk(**kwargs):
    return models.ForeignKey("test_app.Tenant", on_delete=models.CASCADE, **kwargs)


class TestSameSqlAsRLSConstraint:
    """The operation emits the text ``RLSConstraint`` emits for the same table."""

    @staticmethod
    def _op(**options):
        return AddRLSPolicy("Book", field="owner", app_label="test_external_app", **options)

    def test_create_sql_matches_the_constraint(self, state):
        constraint = RLSConstraint(field="owner", name="c")
        assert _forwards_sql(self._op(), state) == str(constraint.create_sql(Book, None))

    def test_drop_sql_matches_the_constraint(self, state):
        constraint = RLSConstraint(field="owner", name="c")
        assert _backwards_sql(self._op(), state) == str(constraint.remove_sql(Book, None))

    def test_every_option_matches_the_constraint(self, state):
        options = {
            "guc_tenant_var": "app.tenant",
            "guc_admin_var": "app.admin",
            "tenant_pk_type": "bigint",
            "extra_bypass_flags": ["app.login", "app.signup"],
        }
        constraint = RLSConstraint(field="owner", name="c", **options)
        sql = _forwards_sql(self._op(**options), state)
        assert sql == str(constraint.create_sql(Book, None))


_LOANED = SharedWith(through="test_app.Loan", fk="book", tenant_fk="tenant")


class TestExtraPredicates:
    """``extra_predicates`` add the sharing policy ``RLSConstraint`` adds (#59)."""

    def test_create_sql_matches_the_constraint(self, state):
        predicates = [_LOANED, RawPredicate("true")]
        constraint = RLSConstraint(field="owner", name="c", extra_predicates=predicates)
        sql = _forwards_sql(_book(extra_predicates=predicates), state)
        assert sql == str(constraint.create_sql(Book, None))
        assert 'CREATE POLICY "test_external_book_tenant_sharing_policy"' in sql

    def test_drop_sql_matches_the_constraint(self, state):
        constraint = RLSConstraint(field="owner", name="c", extra_predicates=[_LOANED])
        sql = _backwards_sql(_book(extra_predicates=[_LOANED]), state)
        assert sql == str(constraint.remove_sql(Book, None))
        assert 'DROP POLICY IF EXISTS "test_external_book_tenant_sharing_policy"' in sql

    def test_without_predicates_there_is_no_sharing_policy(self, state):
        assert "sharing" not in _forwards_sql(_book(), state)
        assert "sharing" not in _backwards_sql(_book(), state)

    def test_sharing_policy_reads_the_settings_when_the_migration_runs(self, state, settings):
        op = _book(extra_predicates=[_LOANED])
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "GUC_PREFIX": "app",
            "TENANT_PK_TYPE": "uuid",
        }
        sharing = _forwards_sql(op, state).split("tenant_sharing_policy")[-1]
        assert "nullif((SELECT current_setting('app.current_tenant', true)), '')::uuid" in sharing

    def test_through_model_is_read_from_the_migration_state(self, state):
        """A model that only the migration state holds, as during ``migrate``."""
        _with_model(
            state,
            "Share",
            [
                ("book", models.ForeignKey("test_external_app.Book", on_delete=models.CASCADE)),
                ("reader", _tenant_fk()),
            ],
        )
        op = _book(
            extra_predicates=[SharedWith(through="ext.Share", fk="book", tenant_fk="reader")]
        )
        sql = _forwards_sql(op, state)
        assert 'FROM "ext_share" AS "rls_shared"' in sql
        assert '"rls_shared"."reader_id" = ' in sql

    def test_predicate_that_cannot_be_built_runs_nothing(self, state):
        op = _book(extra_predicates=[TenantField("title")])
        editor = _editor()
        with pytest.raises(ValueError, match=r"Book\.title is a CharField, not a ForeignKey"):
            op.database_forwards("test_app", editor, state, state)
        editor.execute.assert_not_called()

    def test_an_item_that_is_not_a_predicate_is_refused(self):
        with pytest.raises(TypeError, match="extra_predicates takes Predicate instances"):
            AddRLSPolicy("Book", extra_predicates=["true"])

    def test_deconstruct(self):
        _, _, kwargs = AddRLSPolicy("Book", extra_predicates=[]).deconstruct()
        assert kwargs == {"model_name": "Book"}
        op = AddRLSPolicy("Book", extra_predicates=(_LOANED,))
        _, _, kwargs = op.deconstruct()
        assert kwargs == {"model_name": "Book", "extra_predicates": [_LOANED]}
        assert AddRLSPolicy(**kwargs).deconstruct() == op.deconstruct()

    def test_migration_writer_output(self):
        migration = migrations.Migration("0002_book_policy", "consumer")
        migration.operations = [AddRLSPolicy("Book", extra_predicates=[_LOANED])]
        source = MigrationWriter(migration).as_string()
        assert "import django_rls_tenants.rls.predicates\n" in source
        assert "django_rls_tenants.rls.predicates.SharedWith(" in source


class TestModelWithAnRLSConstraint:
    """The constraint owns a policy of the same name: the operation leaves the table alone."""

    def test_forwards_does_nothing(self, state):
        editor = _editor()
        AddRLSPolicy("Order").database_forwards("test_app", editor, state, state)
        editor.execute.assert_not_called()

    def test_backwards_does_not_drop_the_constraints_policy(self, state):
        editor = _editor()
        AddRLSPolicy("Order").database_backwards("test_app", editor, state, state)
        editor.execute.assert_not_called()

    def test_a_constraint_the_app_adopted_later(self, state):
        """The third-party app adds an RLSConstraint after the project's AddRLSPolicy."""
        op = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        book = state.models["test_external_app", "book"]
        book.options["constraints"] = [RLSConstraint(field="owner", name="book_rls")]
        state.reload_model("test_external_app", "book")
        editor = _editor()
        op.database_backwards("test_app", editor, state, state)
        op.database_forwards("test_app", editor, state, state)
        editor.execute.assert_not_called()


class TestAppLabel:
    """``app_label`` names the model's app; without it the migration's app is used."""

    def test_app_label_resolves_the_model_of_another_app(self, state):
        op = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        sql = _forwards_sql(op, state, app_label="test_app")
        assert 'ALTER TABLE "test_external_book" ENABLE ROW LEVEL SECURITY' in sql

    def test_without_app_label_the_migration_app_is_used(self, state):
        op = AddRLSPolicy("Book", field="owner")
        sql = _forwards_sql(op, state, app_label="test_external_app")
        assert '"test_external_book"' in sql

    def test_model_name_is_case_insensitive(self, state):
        op = AddRLSPolicy("book", field="owner", app_label="test_external_app")
        assert '"test_external_book"' in _forwards_sql(op, state)

    def test_missing_model_names_the_dependency_to_add(self, state):
        op = AddRLSPolicy("Book", field="owner", app_label="test_app")
        with pytest.raises(ValueError, match=r"no model test_app\.Book") as exc_info:
            op.database_forwards("test_app", _editor(), state, state)
        assert "dependencies" in str(exc_info.value)

    def test_missing_model_also_fails_backwards(self, state):
        op = AddRLSPolicy("Missing", app_label="test_external_app")
        with pytest.raises(ValueError, match=r"no model test_external_app\.Missing"):
            op.database_backwards("test_app", _editor(), state, state)

    def test_proxy_model_is_refused_with_its_concrete_model(self, state):
        op = AddRLSPolicy("BookProxy", field="owner", app_label="test_external_app")
        with pytest.raises(ValueError, match=r"proxy model.*test_external_app\.Book\b"):
            op.database_forwards("test_app", _editor(), state, state)


class TestTenantField:
    """The policy reads the column of the tenant foreign key named by ``field``."""

    def test_missing_field_is_refused(self, state):
        op = AddRLSPolicy("Book", app_label="test_external_app")  # field="tenant"
        with pytest.raises(ValueError, match=r"no field 'tenant'.*adds no column"):
            op.database_forwards("test_app", _editor(), state, state)

    def test_field_that_is_not_a_foreign_key_is_refused(self, state):
        op = AddRLSPolicy("Book", field="title", app_label="test_external_app")
        with pytest.raises(ValueError, match=r"is a CharField, not a ForeignKey"):
            op.database_forwards("test_app", _editor(), state, state)

    def test_foreign_key_to_another_model_is_refused(self, state):
        """A policy on ``author_id`` would protect nothing and satisfy ``check_rls``."""
        _with_model(
            state,
            "Post",
            [("author", models.ForeignKey("test_app.TenantUser", on_delete=models.CASCADE))],
        )
        op = AddRLSPolicy("Post", field="author", app_label="ext")
        with pytest.raises(
            ValueError,
            match=(
                r"ext\.Post\.author is a foreign key to test_app\.tenantuser, not to "
                r"RLS_TENANTS\['TENANT_MODEL'\] \(test_app\.Tenant\)"
            ),
        ):
            op.database_forwards("test_app", _editor(), state, state)

    def test_one_to_one_field_is_accepted(self, state):
        _with_model(
            state,
            "Profile",
            [("tenant", models.OneToOneField("test_app.Tenant", on_delete=models.CASCADE))],
        )
        sql = _forwards_sql(AddRLSPolicy("Profile", app_label="ext"), state)
        assert "ELSE tenant_id = " in sql

    def test_db_column_is_used_and_quoted(self, state):
        _with_model(state, "Legacy", [("tenant", _tenant_fk(db_column="org_id"))])
        sql = _forwards_sql(AddRLSPolicy("Legacy", app_label="ext"), state)
        assert 'ELSE "org_id" = ' in sql
        assert "tenant_id" not in sql

    def test_reserved_word_column_is_quoted(self, state):
        """Unquoted, ``user`` would be PostgreSQL's ``current_user``."""
        _with_model(state, "Legacy", [("tenant", _tenant_fk(db_column="user"))])
        sql = _forwards_sql(AddRLSPolicy("Legacy", app_label="ext"), state)
        assert 'ELSE "user" = ' in sql

    def test_django_named_column_is_not_quoted(self, state):
        """``<field>_id`` stays as ``RLSConstraint`` writes it."""
        sql = _forwards_sql(_book(), state)
        assert "ELSE owner_id = " in sql

    def test_column_that_is_not_lower_case_is_quoted(self, state):
        """PostgreSQL folds an unquoted name to lower case; Django created it quoted."""
        _with_model(state, "Legacy", [("tenant", _tenant_fk(db_column="OrgID"))])
        sql = _forwards_sql(AddRLSPolicy("Legacy", app_label="ext"), state)
        assert 'ELSE "OrgID" = ' in sql

    def test_upper_case_django_named_column_is_quoted(self, state):
        """Unquoted, PostgreSQL would fold ``Tenant_id`` to ``tenant_id``."""
        _with_model(state, "Legacy", [("Tenant", _tenant_fk())])
        sql = _forwards_sql(AddRLSPolicy("Legacy", field="Tenant", app_label="ext"), state)
        assert 'ELSE "Tenant_id" = ' in sql

    @pytest.mark.parametrize(
        "table",
        ['other"."legacy', "it's", "ext_$body$", "ext\\legacy"],
        ids=["schema", "quote", "dollar", "backslash"],
    )
    def test_table_that_cannot_be_interpolated_is_refused(self, state, table):
        _with_model(state, "Legacy", [("tenant", _tenant_fk())], db_table=table)
        op = AddRLSPolicy("Legacy", app_label="ext")
        with pytest.raises(ValueError, match=r"quote, a backslash or a dollar sign"):
            op.database_forwards("x", _editor(), state, state)
        with pytest.raises(ValueError, match=r"quote, a backslash or a dollar sign"):
            op.database_backwards("x", _editor(), state, state)

    def test_column_that_cannot_be_interpolated_is_refused(self, state):
        _with_model(state, "Legacy", [("tenant", _tenant_fk(db_column='org"; DROP'))])
        with pytest.raises(ValueError, match=r"the column of ext\.Legacy\.tenant"):
            _forwards_sql(AddRLSPolicy("Legacy", app_label="ext"), state)


class TestDefaultsFromSettings:
    """Unset GUC names and the PK type are read from ``RLS_TENANTS`` when the migration runs."""

    def test_defaults_are_the_configured_ones(self, state):
        sql = _forwards_sql(_book(), state)
        assert "current_setting('rls.current_tenant', true)" in sql
        assert "current_setting('rls.is_admin', true)" in sql
        assert "::int" in sql

    def test_settings_are_read_when_the_migration_runs(self, state, settings):
        op = _book()  # built before the settings change
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "GUC_PREFIX": "app",
            "TENANT_PK_TYPE": "uuid",
        }
        sql = _forwards_sql(op, state)
        assert "current_setting('app.current_tenant', true)" in sql
        assert "current_setting('app.is_admin', true)" in sql
        assert "::uuid" in sql
        assert "'rls." not in sql

    def test_given_values_override_the_settings(self, state, settings):
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "GUC_PREFIX": "app"}
        op = _book(guc_tenant_var="own.tenant", guc_admin_var="own.admin", tenant_pk_type="bigint")
        sql = _forwards_sql(op, state)
        assert "current_setting('own.tenant', true)" in sql
        assert "current_setting('own.admin', true)" in sql
        assert "::bigint" in sql
        assert "'app." not in sql

    @pytest.mark.parametrize(
        ("override", "match"),
        [
            ({"GUC_PREFIX": "rls'; --"}, "guc_tenant_var"),
            ({"TENANT_PK_TYPE": "text"}, "tenant_pk_type"),
        ],
    )
    def test_settings_that_cannot_be_interpolated_are_refused(
        self, state, settings, override, match
    ):
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, **override}
        with pytest.raises(ValueError, match=match):
            _book().database_forwards("test_app", _editor(), state, state)

    def test_extra_bypass_flags_reach_using_only(self, state):
        sql = _forwards_sql(_book(extra_bypass_flags=["rls.login"]), state)
        using, with_check = sql.split("WITH CHECK")
        assert "current_setting('rls.login', true)" in using
        assert "rls.login" not in with_check


class TestRouterAndModelKinds:
    """As in Django's own operations: the router decides by the model; some models are skipped."""

    def test_router_is_asked_with_the_target_model(self, state):
        editor = _editor()
        op = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        with patch.object(router, "allow_migrate_model", return_value=False) as allow:
            op.database_forwards("test_app", editor, state, state)
        allow.assert_called_once()
        alias, model = allow.call_args[0]
        assert alias == "default"
        assert model._meta.label == "test_external_app.Book"
        editor.execute.assert_not_called()

    def test_backwards_skips_a_database_the_router_refuses(self, state):
        editor = _editor()
        op = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        with patch.object(router, "allow_migrate_model", return_value=False):
            op.database_backwards("test_app", editor, state, state)
        editor.execute.assert_not_called()

    def test_unmanaged_model_is_skipped(self, state):
        _with_model(state, "Legacy", [("tenant", _tenant_fk())], managed=False)
        editor = _editor()
        AddRLSPolicy("Legacy", app_label="ext").database_forwards("x", editor, state, state)
        AddRLSPolicy("Legacy", app_label="ext").database_backwards("x", editor, state, state)
        editor.execute.assert_not_called()


class TestValidation:
    """Arguments interpolated into SQL are validated when the operation is built."""

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"model_name": "Book; DROP"}, "model_name"),
            ({"model_name": "Book", "field": "owner id"}, "field"),
            ({"model_name": "Book", "app_label": "a.b"}, "app_label"),
            ({"model_name": "Book", "tenant_pk_type": "text"}, "tenant_pk_type"),
            ({"model_name": "Book", "guc_tenant_var": "x'"}, "guc_tenant_var"),
            ({"model_name": "Book", "guc_admin_var": "x y"}, "guc_admin_var"),
            ({"model_name": "Book", "extra_bypass_flags": ["ok.flag", "bad;"]}, "extra_bypass"),
        ],
    )
    def test_invalid_argument_is_refused(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            AddRLSPolicy(**kwargs)

    def test_model_name_is_positional(self):
        assert AddRLSPolicy("Book").model_name == "Book"
        with pytest.raises(TypeError):
            AddRLSPolicy("Book", "owner")  # type: ignore[misc]


class TestDescribeAndState:
    """The listing text and the (absent) state change."""

    def test_describe_with_app_label(self):
        op = AddRLSPolicy("Book", app_label="library")
        assert op.describe() == "Add RLS policy to library.Book"

    def test_describe_without_app_label(self):
        assert AddRLSPolicy("Book").describe() == "Add RLS policy to Book"

    def test_formatted_description_is_an_addition(self):
        assert AddRLSPolicy("Book").formatted_description() == "+ Add RLS policy to Book"

    def test_reversible_and_reduces_to_sql(self):
        op = AddRLSPolicy("Book")
        assert op.reversible is True
        assert op.reduces_to_sql is True

    def test_state_forwards_changes_nothing(self, state):
        before = state.clone()
        AddRLSPolicy("Book", field="owner", app_label="test_external_app").state_forwards(
            "test_app", state
        )
        assert state.models.keys() == before.models.keys()
        book = state.models["test_external_app", "book"]
        assert book.options.get("constraints", []) == []

    def test_model_name_lower(self):
        assert AddRLSPolicy("BookCopy").model_name_lower == "bookcopy"


class TestDeconstruct:
    """Only the given arguments are serialised."""

    def test_minimal(self):
        name, args, kwargs = AddRLSPolicy("Book").deconstruct()
        assert name == "AddRLSPolicy"
        assert args == []
        assert kwargs == {"model_name": "Book"}

    def test_defaults_given_explicitly_are_omitted_only_for_field(self):
        _, _, kwargs = AddRLSPolicy("Book", field="tenant", extra_bypass_flags=[]).deconstruct()
        assert kwargs == {"model_name": "Book"}

    def test_full_roundtrip(self):
        op = AddRLSPolicy(
            "Book",
            field="owner",
            app_label="library",
            tenant_pk_type="uuid",
            guc_tenant_var="app.tenant",
            guc_admin_var="app.admin",
            extra_bypass_flags=["app.login"],
        )
        _, _, kwargs = op.deconstruct()
        assert kwargs == {
            "model_name": "Book",
            "field": "owner",
            "app_label": "library",
            "tenant_pk_type": "uuid",
            "guc_tenant_var": "app.tenant",
            "guc_admin_var": "app.admin",
            "extra_bypass_flags": ["app.login"],
        }
        assert AddRLSPolicy(**kwargs).deconstruct() == op.deconstruct()

    def test_migration_writer_output(self):
        """``MigrationWriter`` writes the module path once and only the given keywords."""
        migration = migrations.Migration("0002_book_policy", "consumer")
        migration.operations = [AddRLSPolicy("Book", field="owner", app_label="library")]
        source = MigrationWriter(migration).as_string()
        assert "import django_rls_tenants.operations\n" in source
        assert "        django_rls_tenants.operations.AddRLSPolicy(\n" in source
        assert "model_name='Book'" in source or 'model_name="Book"' in source
        assert "app_label=" in source
        assert "guc_tenant_var" not in source
        assert "tenant_pk_type" not in source
