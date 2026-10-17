"""End-to-end tests of ``AddRLSPolicy`` on a model the project does not own (#112).

``test_external_app.Book`` has no ``RLSConstraint``; ``test_app``'s migration
``0010_loan_book_rls_policy`` adds its policy with ``AddRLSPolicy(app_label=...)``
and ``tests/settings.py`` lists it in ``RLS_TENANTS["EXTERNAL_MODELS"]``. The
integration conftest applies ``enforce_rls``, so the policy is what filters.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import ProjectState

from django_rls_tenants.management._rls_migrations import (
    FIELD_MISMATCH,
    NO_POLICIES,
    RLS_NOT_FORCED,
    MigrationFacts,
    model_hint,
)
from django_rls_tenants.operations import AddRLSPolicy
from django_rls_tenants.tenants.context import admin_context, tenant_context
from tests.test_app.models import Loan, Order, TenantUser
from tests.test_external_app.models import Book

pytestmark = pytest.mark.django_db

_TABLE = "test_external_book"
_POLICY = f"{_TABLE}_tenant_isolation_policy"
_MIGRATION = ("test_app", "0010_loan_book_rls_policy")


def _rls_status(table=_TABLE):
    """Return ``(enabled, forced, [policy names])`` of ``table``."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            [table],
        )
        enabled, forced = cursor.fetchone()
        cursor.execute("SELECT policyname FROM pg_policies WHERE tablename = %s", [table])
        policies = sorted(row[0] for row in cursor.fetchall())
    return enabled, forced, policies


def _raw_count():
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{_TABLE}"')
        return cursor.fetchone()[0]


class TestMigratedPolicy:
    """The migration created the policy ``RLSConstraint`` would have created."""

    def test_rls_is_enabled_and_forced_with_the_policy(self):
        assert _rls_status() == (True, True, [_POLICY])

    def test_policy_reads_the_external_tenant_column(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT qual, with_check FROM pg_policies WHERE policyname = %s", [_POLICY]
            )
            using, with_check = cursor.fetchone()
        assert "owner_id = " in using
        assert "owner_id = " in with_check
        assert "tenant_id" not in using

    def test_tenant_sees_only_its_own_rows(self, sample_books, tenant_a, tenant_b):
        """``Book`` has a plain manager: the policy alone filters it."""
        with tenant_context(tenant_a.pk):
            assert list(Book.objects.values_list("title", flat=True)) == ["Book A"]
            assert _raw_count() == 1
        with tenant_context(tenant_b.pk):
            assert list(Book.objects.values_list("title", flat=True)) == ["Book B"]

    def test_admin_sees_every_row(self, sample_books):
        with admin_context():
            assert Book.objects.count() == 2

    def test_no_context_sees_nothing(self, sample_books):
        assert Book.objects.count() == 0
        assert _raw_count() == 0

    def test_insert_into_another_tenant_is_refused(self, tenant_a, tenant_b):
        with (
            tenant_context(tenant_a.pk),
            pytest.raises(DatabaseError, match="row-level security"),
            transaction.atomic(),
        ):
            Book.objects.create(title="Smuggled", owner=tenant_b)

    def test_moving_a_row_to_another_tenant_is_refused(self, sample_books, tenant_a, tenant_b):
        with (
            tenant_context(tenant_a.pk),
            pytest.raises(DatabaseError, match="row-level security"),
            transaction.atomic(),
        ):
            Book.objects.filter(pk=sample_books["a"].pk).update(owner=tenant_b)

    def test_join_from_a_protected_model(self, sample_books, tenant_a):
        """``select_related()`` into the external model filters on ``owner``, and RLS agrees."""
        with tenant_context(tenant_a.pk):
            loans = list(Loan.objects.select_related("book"))
        assert [(loan.borrower, loan.book.title) for loan in loans] == [("Reader A", "Book A")]


class TestReversible:
    """``database_backwards`` drops the policy and turns RLS off; forwards restores both."""

    def test_round_trip(self):
        op = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        state = ProjectState.from_apps(apps)
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")  # the table owner may alter it; enforce_rls's role not
        with connection.schema_editor() as editor:
            op.database_backwards("test_app", editor, state, state)
        assert _rls_status() == (False, False, [])
        with connection.schema_editor() as editor:
            op.database_forwards("test_app", editor, state, state)
        assert _rls_status() == (True, True, [_POLICY])

    def test_forwards_skips_an_existing_policy(self):
        """The DO block creates the policy only when the table has none of that name."""
        op = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        state = ProjectState.from_apps(apps)
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            oid_before = _policy_oid(cursor)
        with connection.schema_editor() as editor:
            op.database_forwards("test_app", editor, state, state)
        assert _rls_status() == (True, True, [_POLICY])
        with connection.cursor() as cursor:
            assert _policy_oid(cursor) == oid_before  # the same row: not dropped and recreated

    def test_sqlmigrate_prints_the_policy_in_both_directions(self):
        """The ``How to fix:`` line tells the user to run ``sqlmigrate``: it shows the DO block."""
        out = StringIO()
        call_command("sqlmigrate", *_MIGRATION, stdout=out)
        assert f'CREATE POLICY "{_POLICY}"' in out.getvalue()
        assert "ELSE owner_id = " in out.getvalue()
        out = StringIO()
        call_command("sqlmigrate", *_MIGRATION, backwards=True, stdout=out)
        assert f'DROP POLICY IF EXISTS "{_POLICY}" ON "{_TABLE}"' in out.getvalue()

    def test_makemigrations_has_nothing_to_write(self):
        """The policy is outside the third-party app's model state: no migration is due."""
        out = StringIO()
        call_command("makemigrations", "--check", "--dry-run", stdout=out)
        assert "No changes detected" in out.getvalue()


@pytest.mark.django_db(transaction=True)
class TestMigrateBackwards:
    """``migrate`` to the migration before ``0010`` drops the policy; forwards puts it back."""

    def test_unapply_and_reapply(self):
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
        executor = MigrationExecutor(connection)
        # Back to the latest migration: those after 0010 are unapplied too.
        leaves = executor.loader.graph.leaf_nodes("test_app")
        try:
            executor.migrate([("test_app", "0009_assignment")])
            assert _rls_status() == (False, False, [])
        finally:
            MigrationExecutor(connection).migrate(leaves)
        assert _rls_status() == (True, True, [_POLICY])


def _other_field_for_order(model):
    """A stand-in ``rls_policy_field()``: Order's policy reads another field than its entry."""
    return "other" if model is Order else "owner"


def _policy_oid(cursor):
    cursor.execute("SELECT oid FROM pg_policy WHERE polname = %s", [_POLICY])
    return cursor.fetchone()[0]


class TestCheckRls:
    """``check_rls`` checks the external table and words its fix from ``AddRLSPolicy``."""

    def test_lists_the_external_table(self):
        out = StringIO()
        call_command("check_rls", stdout=out)
        assert f"  Book ({_TABLE}): {_POLICY}" in out.getvalue()

    def test_dropped_policy_points_at_the_migration(self):
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute(f'DROP POLICY "{_POLICY}" ON "{_TABLE}"')
        err = StringIO()
        with pytest.raises(SystemExit) as exc_info:
            call_command("check_rls", stdout=StringIO(), stderr=err)
        assert exc_info.value.code == 1
        output = err.getvalue()
        assert f"  Book ({_TABLE}): {NO_POLICIES}" in output
        assert f"migration {_MIGRATION[0]}.{_MIGRATION[1]} created its policy" in output
        assert f"`sqlmigrate {_MIGRATION[0]} {_MIGRATION[1]}`" in output

    def test_listed_model_without_a_migration(self, settings):
        """An entry with no ``AddRLSPolicy`` on disk gets the operation to write."""
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {
                **settings.RLS_TENANTS["EXTERNAL_MODELS"],
                "test_app.TenantUser": "tenant",
            },
        }
        err = StringIO()
        with pytest.raises(SystemExit):
            call_command("check_rls", stdout=StringIO(), stderr=err)
        output = err.getvalue()
        assert "  TenantUser (test_user): RLS not enabled" in output
        assert (
            "  TenantUser (test_user): RLS_TENANTS['EXTERNAL_MODELS'] lists "
            "test_app.TenantUser, but no migration has an AddRLSPolicy for it. Add "
            "AddRLSPolicy('TenantUser', app_label='test_app') to a migration of your own "
            "app that depends on the 'test_app' migration creating 'tenant', then run "
            "migrate." in output
        )


class TestCheckRlsComparesWithTheSetting:
    """``check_rls`` compares the ``AddRLSPolicy`` operations on disk with EXTERNAL_MODELS."""

    def test_entry_naming_another_field_than_the_policy_is_an_issue(self, settings):
        """The FK check and the join filters would read another column than the policy."""
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {"test_external_app.Book": "title"},
        }
        err = StringIO()
        with pytest.raises(SystemExit) as exc_info:
            call_command("check_rls", stdout=StringIO(), stderr=err)
        assert exc_info.value.code == 1
        output = err.getvalue()
        assert f"  Book ({_TABLE}): {FIELD_MISMATCH}" in output
        assert (
            f"  Book ({_TABLE}): the AddRLSPolicy of migration {_MIGRATION[0]}.{_MIGRATION[1]} "
            f"reads 'owner', but RLS_TENANTS['EXTERNAL_MODELS'] maps test_external_app.Book "
            f"to 'title'" in output
        )
        assert f'DROP POLICY "{_POLICY}" ON "{_TABLE}"' in output

    def test_protected_model_the_setting_does_not_list_gets_a_warning(self, settings):
        """The table is protected, so the run passes; the runtime does not know it."""
        settings.RLS_TENANTS = {
            key: value for key, value in settings.RLS_TENANTS.items() if key != "EXTERNAL_MODELS"
        }
        out, err = StringIO(), StringIO()
        call_command("check_rls", stdout=out, stderr=err)
        assert "verified" in out.getvalue()
        assert f"Book ({_TABLE})" not in out.getvalue()
        assert (
            f"test_external_app.Book ({_TABLE}): migration {_MIGRATION[0]}.{_MIGRATION[1]} has "
            f"an AddRLSPolicy for it, but RLS_TENANTS['EXTERNAL_MODELS'] does not list it"
            in err.getvalue()
        )
        assert "Add 'test_external_app.Book': 'owner' to" in err.getvalue()

    def test_listed_model_gets_no_warning(self):
        err = StringIO()
        call_command("check_rls", stdout=StringIO(), stderr=err)
        assert err.getvalue() == ""

    def test_policy_of_a_removed_model_or_of_a_constraint_model_is_not_reported(self):
        """An AddRLSPolicy for a model that is gone, or that adopted an RLSConstraint, is moot."""
        err = StringIO()
        with (
            patch.object(
                MigrationFacts,
                "rls_policy_models",
                return_value=[("test_app", "order"), ("gone", "thing")],
            ),
            patch.object(
                MigrationFacts,
                "rls_policy_migrations",
                return_value=[("test_app", "0001_initial")],
            ),
            patch.object(MigrationFacts, "rls_policy_field", side_effect=_other_field_for_order),
        ):
            call_command("check_rls", stdout=StringIO(), stderr=err)
        assert err.getvalue() == ""

    def test_listed_model_with_a_constraint_gets_no_field_mismatch(self, settings):
        """The constraint's policy is the one in force (W016 reports the entry)."""
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {**settings.RLS_TENANTS["EXTERNAL_MODELS"], "test_app.Order": "x"},
        }
        err = StringIO()
        with patch.object(MigrationFacts, "rls_policy_field", side_effect=_other_field_for_order):
            call_command("check_rls", stdout=StringIO(), stderr=err)  # does not exit 1
        assert FIELD_MISMATCH not in err.getvalue()

    def test_generate_fails_when_the_migrations_cannot_be_read(self):
        """``--generate`` needs the migrations: no degrading to advice."""
        with (
            patch(
                "django_rls_tenants.management.commands.check_rls.MigrationFacts",
                side_effect=RuntimeError("broken migration module"),
            ),
            pytest.raises(RuntimeError, match="broken migration module"),
        ):
            call_command(
                "check_rls", "--generate", "--dry-run", stdout=StringIO(), stderr=StringIO()
            )

    def test_migrations_that_cannot_be_read_do_not_fail_the_run(self):
        err = StringIO()
        with patch(
            "django_rls_tenants.management.commands.check_rls.MigrationFacts",
            side_effect=RuntimeError("broken migration module"),
        ):
            call_command("check_rls", stdout=StringIO(), stderr=err)
        assert (
            "Could not read the migrations (to compare their AddRLSPolicy operations with "
            "RLS_TENANTS['EXTERNAL_MODELS'], and to tell how to fix any issues): "
            "RuntimeError: broken migration module." in err.getvalue()
        )

    def test_warning_is_printed_when_no_model_is_protected(self, settings):
        """The orphan ``AddRLSPolicy`` is the only protection: it must be reported too."""
        settings.RLS_TENANTS = {
            key: value for key, value in settings.RLS_TENANTS.items() if key != "EXTERNAL_MODELS"
        }
        out, err = StringIO(), StringIO()
        with (
            patch(
                "django_rls_tenants.management.commands.check_rls._collect_rls_models",
                return_value={},
            ),
            patch(
                "django_rls_tenants.management.commands.check_rls._collect_m2m_tables",
                return_value={},
            ),
        ):
            call_command("check_rls", stdout=out, stderr=err)
        assert "No RLS-protected models found." in out.getvalue()
        assert f"test_external_app.Book ({_TABLE}): migration" in err.getvalue()
        assert "Add 'test_external_app.Book': 'owner' to" in err.getvalue()


class TestExternalModelHints:
    """``model_hint()`` for a model listed in EXTERNAL_MODELS, from ``MigrationFacts``."""

    @pytest.fixture
    def facts(self):
        return MigrationFacts("default")

    def test_migrations_are_recorded_in_dependency_order(self, facts):
        """Every migration comes after its dependencies, whatever the apps' names."""
        order = facts._migration_order()
        assert sorted(order) == sorted(facts._graph.nodes)
        position = {key: index for index, key in enumerate(order)}
        for key, node in facts._graph.node_map.items():
            for parent in node.parents:
                assert position[parent.key] < position[key], (parent.key, key)
        # The policy of the external app's model comes from test_app's migration,
        # which depends on the external app's: a cross-app order sorted() gets wrong.
        assert position[("test_external_app", "0001_initial")] < position[_MIGRATION]

    def test_facts_record_the_operation_by_its_target_app(self, facts):
        assert facts.rls_policy_migrations(Book) == [_MIGRATION]
        assert facts.rls_policy_migrations(Loan) == []
        assert facts.rls_policy_migrations(Order) == []
        assert facts.rls_policy_field(Book) == "owner"
        assert facts.rls_policy_field(Order) is None
        assert ("test_external_app", "book") in facts.rls_policy_models()

    def test_several_operations_for_one_model(self, facts):
        """Without ``app_label`` the migration's app is the model's; the latest field wins."""
        facts._record(("test_app", "9998_a"), AddRLSPolicy("Order"))
        facts._record(("test_app", "9999_b"), AddRLSPolicy("Order", field="other"))
        assert facts.rls_policy_migrations(Order) == [
            ("test_app", "9998_a"),
            ("test_app", "9999_b"),
        ]
        assert facts.rls_policy_field(Order) == "other"

    def test_hints_pick_the_right_migration_of_several(self, facts, settings):
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {"test_app.TenantUser": "tenant"},
        }
        first, second = ("test_app", "9998_a"), ("test_app", "9999_b")
        facts._record(first, AddRLSPolicy("TenantUser"))
        facts._record(second, AddRLSPolicy("TenantUser", field="owner"))
        # A mismatch names the latest operation and its field.
        hint = model_hint(facts, TenantUser, {FIELD_MISMATCH})
        assert "migration test_app.9999_b reads 'owner'" in hint
        # Of two unapplied migrations, the first to apply is named.
        with patch.object(facts, "is_applied", return_value=False):
            hint = model_hint(facts, TenantUser, {NO_POLICIES})
        assert hint.startswith("migration test_app.9998_a adds its policy")
        # Both applied and the policy gone: the latest is the one to replay.
        with patch.object(facts, "is_applied", return_value=True):
            hint = model_hint(facts, TenantUser, {NO_POLICIES})
        assert "migration test_app.9999_b created its policy" in hint
        assert "`sqlmigrate test_app 9999_b`" in hint

    def test_unapplied_migration(self, facts):
        with patch.object(facts, "is_applied", return_value=False):
            hint = model_hint(facts, Book, {NO_POLICIES})
        assert hint == (
            f"migration {_MIGRATION[0]}.{_MIGRATION[1]} adds its policy (AddRLSPolicy) and "
            f"is not applied on database 'default': run migrate."
        )

    def test_rls_turned_off(self, facts):
        hint = model_hint(facts, Book, {RLS_NOT_FORCED})
        assert hint.endswith(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY;')

    def test_non_default_field_is_named_in_the_operation(self, facts, settings):
        settings.RLS_TENANTS = {
            **settings.RLS_TENANTS,
            "EXTERNAL_MODELS": {"test_app.TenantUser": "owner"},
        }
        hint = model_hint(facts, TenantUser, {NO_POLICIES})
        assert "AddRLSPolicy('TenantUser', app_label='test_app', field='owner')" in hint

    def test_unmanaged_model(self, facts):
        """``AddRLSPolicy`` does nothing for it, so neither migrate nor a migration helps."""
        with patch.object(Book._meta, "managed", False):
            hint = model_hint(facts, Book, {NO_POLICIES})
        assert "unmanaged (Meta.managed = False)" in hint
        assert "create the policy by hand" in hint

    def test_unmanaged_model_with_rls_turned_off(self, facts):
        """The policy exists: turning RLS back on is the fix, as for any table."""
        with patch.object(Book._meta, "managed", False):
            hint = model_hint(facts, Book, {RLS_NOT_FORCED})
        assert "Turn it back on" in hint
