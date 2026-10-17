"""End-to-end tests of ``extra_predicates`` and ``including_shared()`` (#59).

``test_app.SharedNote``'s ``RLSConstraint`` shares a note with the tenant a
``NoteShare`` names (``SharedWith``) and with its ``partner`` (``TenantField``);
``NoteShare``'s own constraint lets the tenant it shares with read it
(``TenantField("shared_with")``). Migration ``0011_shared_note`` creates both
sharing policies. The integration conftest applies ``enforce_rls``, so the
policies are what filters.
"""

from __future__ import annotations

from contextlib import contextmanager
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.migrations.state import ProjectState

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.operations import AddRLSPolicy
from django_rls_tenants.rls.predicates import RawPredicate, SharedWith
from django_rls_tenants.tenants.context import admin_context, tenant_context
from tests.test_app.models import Loan, NoteShare, Order, SharedNote, Tenant
from tests.test_external_app.models import Book

pytestmark = pytest.mark.django_db

_TABLE = "test_shared_note"
_ISOLATION = f"{_TABLE}_tenant_isolation_policy"
_SHARING = f"{_TABLE}_tenant_sharing_policy"


@pytest.fixture
def tenant_c(db):
    return Tenant.objects.create(name="Tenant C")


@pytest.fixture
def notes(tenant_a, tenant_b):
    """Notes of A shared with B (through a share, as its partner), a private one, B's own."""
    with admin_context():
        shared = SharedNote.objects.create(tenant=tenant_a, title="A shared")
        partner = SharedNote.objects.create(tenant=tenant_a, title="A partner", partner=tenant_b)
        private = SharedNote.objects.create(tenant=tenant_a, title="A private")
        own = SharedNote.objects.create(tenant=tenant_b, title="B own")
        share = NoteShare.objects.create(tenant=tenant_a, note=shared, shared_with=tenant_b)
        # B shares its own note with A: B owns the share, so B sees it as its own row.
        own_share = NoteShare.objects.create(tenant=tenant_b, note=own, shared_with=tenant_a)
    return {
        "shared": shared,
        "partner": partner,
        "private": private,
        "own": own,
        "share": share,
        "own_share": own_share,
    }


def _titles(queryset):
    return sorted(queryset.values_list("title", flat=True))


def _raw(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount, (cursor.fetchall() if cursor.description else None)


@contextmanager
def _as_table_owner():
    """Run DDL as the session role, then go back to the role ``enforce_rls`` set."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user::text, session_user::text")
        current, session = cursor.fetchone()
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        if current != session:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('role', %s, false)", [current])


def _policies(table=_TABLE):
    """Return ``{policy name: (permissive, cmd)}`` of ``table``."""
    rows = _raw(
        "SELECT policyname, permissive, cmd FROM pg_policies WHERE tablename = %s", [table]
    )[1]
    return {name: (permissive, cmd) for name, permissive, cmd in rows}


class TestMigratedPolicies:
    """Migration ``0011`` created a ``FOR SELECT`` sharing policy next to the tenant policy."""

    def test_shared_note_policies(self):
        assert _policies() == {
            _ISOLATION: ("PERMISSIVE", "ALL"),
            _SHARING: ("PERMISSIVE", "SELECT"),
        }

    def test_note_share_policies(self):
        assert _policies("test_note_share") == {
            "test_note_share_tenant_isolation_policy": ("PERMISSIVE", "ALL"),
            "test_note_share_tenant_sharing_policy": ("PERMISSIVE", "SELECT"),
        }

    def test_sharing_policy_holds_both_predicates(self):
        [(using,)] = _raw("SELECT qual FROM pg_policies WHERE policyname = %s", [_SHARING])[1]
        assert "FROM test_note_share rls_shared" in using
        assert "rls_shared.note_id = test_shared_note.id" in using
        assert "partner_id = (NULLIF(" in using  # the policy's own table is not qualified

    def test_sqlmigrate_prints_the_sharing_policy(self):
        out = StringIO()
        call_command("sqlmigrate", "test_app", "0011_shared_note", stdout=out)
        assert f'CREATE POLICY "{_SHARING}"' in out.getvalue()
        assert "FOR SELECT" in out.getvalue()

    def test_check_rls_lists_the_sharing_policy(self):
        out = StringIO()
        call_command("check_rls", "--verbose", stdout=out)
        assert f"{_SHARING} [SELECT]" in out.getvalue()


class TestReading:
    """The policies let a tenant read the rows the predicates share with it, and no more."""

    def test_raw_sql_sees_own_and_shared_rows(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            rows = _raw(f'SELECT title FROM "{_TABLE}" ORDER BY title')[1]
        assert rows == [("A partner",), ("A shared",), ("B own",)]

    def test_default_manager_keeps_the_tenant_filter(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            assert _titles(SharedNote.objects.all()) == ["B own"]

    def test_including_shared(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            qs = SharedNote.objects.including_shared()
            assert _titles(qs) == ["A partner", "A shared", "B own"]
            assert qs.count() == 3
            assert qs.filter(title__startswith="A").exists()
            assert _titles(qs.filter(title__startswith="A")) == ["A partner", "A shared"]

    def test_owner_reads_its_own_rows_only(self, notes, tenant_a):
        """B's own note is shared with A through B's share."""
        with tenant_context(tenant_a.pk):
            assert _titles(SharedNote.objects.including_shared()) == [
                "A partner",
                "A private",
                "A shared",
                "B own",
            ]

    def test_tenant_nothing_is_shared_with(self, notes, tenant_c):
        with tenant_context(tenant_c.pk):
            assert list(SharedNote.objects.including_shared()) == []
            assert _raw(f'SELECT count(*) FROM "{_TABLE}"')[1] == [(0,)]

    def test_admin_reads_everything(self, notes):
        with admin_context():
            assert SharedNote.objects.including_shared().count() == 4

    def test_no_context_reads_nothing(self, notes):
        assert list(SharedNote.objects.including_shared()) == []

    def test_strict_mode_guards_it(self, notes, settings):
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "STRICT_MODE": True}
        with pytest.raises(NoTenantContextError):
            list(SharedNote.objects.including_shared())

    def test_for_user_after_including_shared(self, notes, tenant_b_user):
        qs = SharedNote.objects.including_shared().for_user(tenant_b_user)
        assert _titles(qs) == ["A partner", "A shared", "B own"]
        assert _titles(SharedNote.objects.for_user(tenant_b_user)) == ["B own"]

    def test_share_rows_are_read_through_their_own_predicate(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            assert list(NoteShare.objects.all()) == [notes["own_share"]]
            shares = NoteShare.objects.including_shared().order_by("pk")
            assert list(shares) == [notes["share"], notes["own_share"]]


class TestRelatedRows:
    """No tenant filter is added on joins and prefetches: the policies decide."""

    def test_select_related(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            share = (
                NoteShare.objects.including_shared()
                .select_related("note")
                .get(pk=notes["share"].pk)
            )
            with self._no_queries():
                assert share.note.title == "A shared"

    def test_prefetch_related_forward_foreign_key(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            shares = list(
                NoteShare.objects.including_shared().prefetch_related("note").order_by("pk")
            )
            with self._no_queries():
                assert [share.note.title for share in shares] == ["A shared", "B own"]

    def test_related_manager(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            note = SharedNote.objects.including_shared().get(pk=notes["shared"].pk)
            assert list(note.shares.all()) == []  # the default manager's tenant filter
            assert list(note.shares.including_shared()) == [notes["share"]]

    def test_related_manager_with_prefetched_rows(self, notes, tenant_b):
        """The prefetched rows were read with the tenant filter: they are not reused."""
        with tenant_context(tenant_b.pk):
            note = (
                SharedNote.objects.including_shared()
                .prefetch_related("shares")
                .get(pk=notes["shared"].pk)
            )
            assert list(note.shares.all()) == []
            assert list(note.shares.including_shared()) == [notes["share"]]

    @staticmethod
    @contextmanager
    def _no_queries():
        from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

        with CaptureQueriesContext(connection) as queries:
            yield
        assert len(queries) == 0


class TestWriting:
    """The sharing policy is ``FOR SELECT``: shared rows cannot be changed, locked or taken."""

    def test_update_delete_and_takeover_affect_nothing(self, notes, tenant_b):
        pk = notes["shared"].pk
        statements = [
            (f"UPDATE \"{_TABLE}\" SET title = 'x' WHERE id = %s", [pk]),
            (f'UPDATE "{_TABLE}" SET tenant_id = %s WHERE id = %s', [tenant_b.pk, pk]),
            (f'DELETE FROM "{_TABLE}" WHERE id = %s', [pk]),
        ]
        with tenant_context(tenant_b.pk), transaction.atomic():
            assert [_raw(sql, params)[0] for sql, params in statements] == [0, 0, 0]
            locked = _raw(f'SELECT id FROM "{_TABLE}" WHERE id = %s FOR UPDATE', [pk])
            assert locked[1] == []
        with admin_context():
            note = SharedNote.objects.get(pk=pk)
        assert (note.title, note.tenant_id) == ("A shared", notes["shared"].tenant_id)

    def test_insert_into_another_tenant_is_refused(self, notes, tenant_a, tenant_b):
        with tenant_context(tenant_b.pk), pytest.raises(DatabaseError), transaction.atomic():
            SharedNote.objects.create(tenant=tenant_a, title="planted")

    def test_saving_a_shared_row_fails(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            note = SharedNote.objects.including_shared().get(pk=notes["shared"].pk)
            note.title = "changed"
            with pytest.raises(DatabaseError), transaction.atomic():
                note.save()
        with admin_context():
            assert SharedNote.objects.get(pk=notes["shared"].pk).title == "A shared"

    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("update", lambda qs, notes: qs.update(title="x")),
            ("delete", lambda qs, notes: qs.delete()),
            ("bulk_update", lambda qs, notes: qs.bulk_update([notes["own"]], ["title"])),
            ("update_or_create", lambda qs, notes: qs.update_or_create(title="B own")),
            ("select_for_update", lambda qs, notes: qs.select_for_update()),
        ],
    )
    def test_writes_are_refused(self, notes, tenant_b, method, call):
        with tenant_context(tenant_b.pk):
            qs = SharedNote.objects.including_shared().filter(title="B own")
            with pytest.raises(TypeError, match=rf"Cannot call {method}\(\) on an including"):
                call(qs, notes)
            assert _titles(SharedNote.objects.including_shared()) == [
                "A partner",
                "A shared",
                "B own",
            ]

    def test_refused_on_clones_and_related_managers(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            with pytest.raises(TypeError, match="Cannot call delete"):
                SharedNote.objects.including_shared().filter(pk=0).values("pk").delete()
            note = SharedNote.objects.get(pk=notes["own"].pk)
            with pytest.raises(TypeError, match="Cannot call update"):
                note.shares.including_shared().update(shared_with=tenant_b)

    @pytest.mark.parametrize("operator", ["__or__", "__and__", "__xor__"])
    def test_refused_on_a_combination_with_shared_rows(self, notes, tenant_b, operator):
        """Django combines from a clone of the left operand: the right one's flag is kept."""
        with tenant_context(tenant_b.pk):
            own = SharedNote.objects.filter(title="B own")
            combined = getattr(own, operator)(SharedNote.objects.including_shared())
            with pytest.raises(TypeError, match="Cannot call delete"):
                combined.delete()
            assert not getattr(own, operator)(SharedNote.objects.all())._rls_including_shared

    @pytest.mark.parametrize("operator", ["__or__", "__xor__"])
    def test_combination_with_none_marks_a_clone_not_the_operand(self, notes, tenant_b, operator):
        """Django returns the left operand itself when the right one is ``none()``."""
        with tenant_context(tenant_b.pk):
            own = SharedNote.objects.filter(title="B own")
            combined = getattr(own, operator)(SharedNote.objects.including_shared().none())
            assert combined._rls_including_shared
            assert combined is not own
            assert not own._rls_including_shared
            assert own.update(title="B own") == 1

    def test_empty_left_operand_is_not_marked(self, notes, tenant_b):
        """``none() & shared``: Django returns the (empty) left operand itself."""
        with tenant_context(tenant_b.pk):
            empty = SharedNote.objects.filter(title="B own").none()
            combined = empty & SharedNote.objects.including_shared()
            assert combined._rls_including_shared
            assert combined is not empty
            assert not empty._rls_including_shared
            assert list(combined) == []

    def test_writes_without_including_shared_still_work(self, notes, tenant_b):
        with tenant_context(tenant_b.pk):
            assert SharedNote.objects.filter(title="B own").update(title="B renamed") == 1


class TestRefusedModels:
    def test_model_without_predicates(self):
        with pytest.raises(RLSConfigurationError, match=r"test_app\.Order: its RLSConstraint has"):
            Order.objects.including_shared()

    def test_hint_names_the_fix(self):
        with pytest.raises(RLSConfigurationError) as excinfo:
            Order.objects.including_shared()
        assert "Add extra_predicates (SharedWith, TenantField, RawPredicate)" in excinfo.value.hint


class TestConstraintRoundTrip:
    """``RemoveConstraint`` drops both policies; ``AddConstraint`` creates both."""

    def test_remove_and_add(self):
        [constraint] = SharedNote._meta.constraints
        with _as_table_owner():
            with connection.schema_editor() as editor:
                editor.remove_constraint(SharedNote, constraint)
            assert _policies() == {}
            with connection.schema_editor() as editor:
                editor.add_constraint(SharedNote, constraint)
        assert set(_policies()) == {_ISOLATION, _SHARING}


class TestAddRLSPolicy:
    """``AddRLSPolicy(extra_predicates=...)`` on the external ``Book``: loans share books."""

    _LOANED = SharedWith(through="test_app.Loan", fk="book", tenant_fk="tenant")

    @pytest.fixture
    def loaned_book(self, tenant_a, tenant_b):
        with admin_context():
            book = Book.objects.create(title="A's book", owner=tenant_a)
            Book.objects.create(title="A's other book", owner=tenant_a)
            Loan.objects.create(tenant=tenant_b, borrower="Reader B", book=book)
        return book

    def _replace_policy(self, old, new):
        state = ProjectState.from_apps(apps)
        with _as_table_owner(), connection.schema_editor() as editor:
            # The loan's foreign key check is deferred: ALTER TABLE refuses to run
            # while it is pending.
            editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            old.database_backwards("test_app", editor, state, state)
            new.database_forwards("test_app", editor, state, state)

    def test_forwards_and_backwards(self, loaned_book, tenant_b):
        plain = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        sharing = AddRLSPolicy(
            "Book", field="owner", app_label="test_external_app", extra_predicates=[self._LOANED]
        )
        self._replace_policy(plain, sharing)
        assert _policies("test_external_book") == {
            "test_external_book_tenant_isolation_policy": ("PERMISSIVE", "ALL"),
            "test_external_book_tenant_sharing_policy": ("PERMISSIVE", "SELECT"),
        }
        with tenant_context(tenant_b.pk):
            # Book has a plain manager: the policies alone filter it.
            assert list(Book.objects.values_list("title", flat=True)) == ["A's book"]
        self._replace_policy(sharing, plain)
        assert set(_policies("test_external_book")) == {
            "test_external_book_tenant_isolation_policy"
        }
        with tenant_context(tenant_b.pk):
            assert list(Book.objects.all()) == []

    def test_raw_predicate_with_a_like_pattern_migrates(self, loaned_book, tenant_b):
        """The schema editor composes the DDL: a ``%`` must reach PostgreSQL as one ``%``."""
        plain = AddRLSPolicy("Book", field="owner", app_label="test_external_app")
        public = AddRLSPolicy(
            "Book",
            field="owner",
            app_label="test_external_app",
            extra_predicates=[RawPredicate("title LIKE '%other book'")],
        )
        self._replace_policy(plain, public)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT qual FROM pg_policies WHERE policyname = %s",
                    ["test_external_book_tenant_sharing_policy"],
                )
                [(qual,)] = cursor.fetchall()
            assert "'%other book'" in qual
            with tenant_context(tenant_b.pk):
                assert list(Book.objects.values_list("title", flat=True)) == ["A's other book"]
        finally:
            self._replace_policy(public, plain)
