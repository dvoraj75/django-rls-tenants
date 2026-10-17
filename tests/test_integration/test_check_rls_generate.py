"""Tests for ``check_rls --generate`` and its hints (#46).

The test app's M2M policies come from the hand-written
``0005_m2m_rls_policies``. The ``migrations_without_m2m_policies`` fixture
copies the test app's migrations into a package of their own, without that
migration, and points ``MIGRATION_MODULES`` at it: ``--generate`` must then
write a migration with the same operations, and applying it must give
``check_rls`` a clean run.
"""

from __future__ import annotations

import importlib
import uuid
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.test.utils import isolate_apps

from django_rls_tenants.management._rls_migrations import (
    NO_POLICIES,
    RLS_NOT_ENABLED,
    TABLE_MISSING,
    MigrationFacts,
    _database_operations,
    m2m_hint,
    model_hint,
    plan_m2m_migrations,
)
from django_rls_tenants.management.commands.check_rls import (
    _collect_m2m_tables,
    _collect_rls_models,
)
from django_rls_tenants.operations import AddM2MRLSPolicy
from django_rls_tenants.rls.constraints import RLSConstraint, _build_m2m_drop_sql
from django_rls_tenants.tenants.conf import RLSTenantsConfig
from tests.test_app import migrations as test_app_migrations
from tests.test_app.models import Order, Project

pytestmark = pytest.mark.django_db

_M2M_TABLES = ("test_project_members", "test_project_tags", "test_selfref_friends")
_HAND_WRITTEN = "0005_m2m_rls_policies"
# The test app's two latest migrations (zero-padded names sort in order): the generated
# migration is numbered after the leaf and depends on it.
_PARENT, _LEAF = sorted(
    source.stem for source in Path(test_app_migrations.__file__).parent.glob("0*.py")
)[-2:]
_LEAF_NUMBER = int(_LEAF.split("_", 1)[0])
_GENERATED = f"{_LEAF_NUMBER + 1:04d}_m2m_rls_policies"


@pytest.fixture
def migrations_without_m2m_policies(tmp_path, monkeypatch):
    """Serve the test app's migrations from a package without ``0005_m2m_rls_policies``."""
    package = f"rls_migrations_{uuid.uuid4().hex}"
    directory = tmp_path / package
    directory.mkdir()
    for source in Path(test_app_migrations.__file__).parent.glob("*.py"):
        if source.stem == _HAND_WRITTEN:
            continue
        text = source.read_text().replace(
            f'("test_app", "{_HAND_WRITTEN}")', '("test_app", "0004_tag_selfrefmodel_project")'
        )
        (directory / source.name).write_text(text)
    monkeypatch.syspath_prepend(str(tmp_path))
    with override_settings(MIGRATION_MODULES={"test_app": package}):
        yield directory


def _check_rls(*args):
    """Run ``check_rls``; return ``(exit code, stdout, stderr)``."""
    out = StringIO()
    err = StringIO()
    try:
        call_command("check_rls", *args, stdout=out, stderr=err)
    except SystemExit as exc:
        return exc.code, out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


def _execute(*statements):
    """Run ``statements`` as the table owner (``enforce_rls`` switched to a plain role)."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        for statement in statements:
            cursor.execute(statement)


def _drop_m2m_policies(*tables):
    _execute(*(_build_m2m_drop_sql(table=table) for table in tables))


def _operations(module_name):
    """Return ``{m2m_table: deconstruct()}`` for the operations of migration ``module_name``."""
    importlib.invalidate_caches()
    module = importlib.import_module(module_name)
    return {
        operation.m2m_table: operation.deconstruct() for operation in module.Migration.operations
    }


class TestGenerate:
    """``--generate`` writes the ``AddM2MRLSPolicy`` migrations no migration on disk has."""

    def test_nothing_to_generate_when_a_migration_covers_every_table(self):
        code, out, err = _check_rls("--generate")
        assert code == 0
        assert "No migration to generate" in out
        assert "Migrations for" not in out
        assert err == ""

    def test_writes_the_hand_written_operations(self, migrations_without_m2m_policies):
        """The database has the policies (as from ``setup_m2m_rls``), no migration adds them."""
        code, out, err = _check_rls("--generate")
        assert code == 0  # the database is protected; only the migration was missing
        path = migrations_without_m2m_policies / f"{_GENERATED}.py"
        assert path.is_file()
        assert "Migrations for 'test_app':" in out
        assert f"{_GENERATED}.py" in out
        for table in _M2M_TABLES:
            assert f"    + Add M2M RLS policy to {table}\n" in out
        assert err == ""

        package = migrations_without_m2m_policies.name
        module = importlib.import_module(f"{package}.{_GENERATED}")
        assert module.Migration.dependencies == [("test_app", _LEAF)]
        assert _operations(f"{package}.{_GENERATED}") == _operations(
            f"tests.test_app.migrations.{_HAND_WRITTEN}"
        )

    def test_generated_migration_applies_and_protects(self, migrations_without_m2m_policies):
        _drop_m2m_policies(*_M2M_TABLES)
        code, _, err = _check_rls("--generate")
        assert code == 1  # the database is still unprotected until migrate runs
        for table in _M2M_TABLES:
            assert f"({table}): no RLS policies found" in err
            assert (
                f"({table}): check_rls --generate wrote migration "
                f"test_app.{_GENERATED}, which adds its policy: run migrate.\n"
            ) in err

        importlib.invalidate_caches()
        MigrationExecutor(connection).migrate([("test_app", _GENERATED)])

        code, out, err = _check_rls("--generate")
        assert code == 0, err
        assert "All " in out
        assert "No migration to generate" in out

    def test_dry_run_prints_and_writes_nothing(self, migrations_without_m2m_policies):
        code, out, _ = _check_rls("--generate", "--dry-run")
        assert code == 0
        assert not (migrations_without_m2m_policies / f"{_GENERATED}.py").exists()
        assert "django_rls_tenants.operations.AddM2MRLSPolicy(" in out
        assert "m2m_table='test_project_tags'" in out
        assert "to_tenant_fk=None" in out

    def test_dry_run_hint(self, migrations_without_m2m_policies):
        _drop_m2m_policies("test_project_tags")
        code, _, err = _check_rls("--generate", "--dry-run")
        assert code == 1
        assert (
            f"check_rls --generate --dry-run printed migration test_app.{_GENERATED}, "
            f"which adds its policy: run check_rls --generate to write it, then migrate.\n"
        ) in err

    def test_name(self, migrations_without_m2m_policies):
        _check_rls("--generate", "--name", "protect_m2m")
        named = f"{_LEAF_NUMBER + 1:04d}_protect_m2m.py"
        assert (migrations_without_m2m_policies / named).is_file()

    @override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "GUC_PREFIX": "myco"})
    def test_guc_names_come_from_settings(self, migrations_without_m2m_policies):
        _, out, _ = _check_rls("--generate", "--dry-run")
        assert "guc_tenant_var='myco.current_tenant'" in out
        assert "guc_admin_var='myco.is_admin'" in out

    @override_settings(RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "GUC_PREFIX": "bad prefix"})
    def test_invalid_guc_prefix_is_a_command_error(self, migrations_without_m2m_policies):
        with pytest.raises(CommandError, match="Invalid GUC name"):
            call_command("check_rls", "--generate", stdout=StringIO(), stderr=StringIO())

    def test_skips_a_table_with_a_policy_of_another_name(self, migrations_without_m2m_policies):
        _execute(
            'ALTER POLICY "test_project_tags_m2m_rls_policy" ON "test_project_tags" '
            'RENAME TO "tags_by_hand"'
        )
        code, _, err = _check_rls("--generate")
        assert code == 0
        assert (
            "No migration generated for Project.tags (auto M2M) (test_project_tags): the "
            "table has a policy of another name on database 'default' (tags_by_hand), and "
            "AddM2MRLSPolicy would add a second one."
        ) in err
        package = migrations_without_m2m_policies.name
        assert set(_operations(f"{package}.{_GENERATED}")) == {
            "test_project_members",
            "test_selfref_friends",
        }

    def test_skips_an_app_with_conflicting_migrations(self, migrations_without_m2m_policies):
        conflict = f"{_LEAF_NUMBER:04d}_conflict"  # a second leaf, beside _LEAF
        (migrations_without_m2m_policies / f"{conflict}.py").write_text(
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            f'    dependencies = [("test_app", "{_PARENT}")]\n'
        )
        code, out, err = _check_rls("--generate")
        assert code == 0
        assert "Migrations for" not in out
        assert not (migrations_without_m2m_policies / f"{_GENERATED}.py").exists()
        leaves = ", ".join(f"test_app.{name}" for name in sorted([conflict, _LEAF]))
        for table in _M2M_TABLES:
            assert (
                f"({table}): app 'test_app' has conflicting migrations ({leaves}): run "
                f"makemigrations --merge first."
            ) in err

    def test_refuses_to_overwrite(self, migrations_without_m2m_policies):
        """A file at the planned path is never overwritten (numbered to collide with the leaf)."""
        existing = migrations_without_m2m_policies / f"{_LEAF}.py"
        before = existing.read_text()
        with (
            patch.object(MigrationAutodetector, "parse_number", return_value=_LEAF_NUMBER - 1),
            pytest.raises(CommandError, match=rf"{_LEAF}\.py exists already"),
        ):
            call_command(
                "check_rls",
                "--generate",
                "--name",
                _LEAF.split("_", 1)[1],
                stdout=StringIO(),
                stderr=StringIO(),
            )
        assert existing.read_text() == before

    def test_dry_run_and_name_need_generate(self):
        with pytest.raises(CommandError, match="only apply with --generate"):
            call_command("check_rls", "--dry-run", stdout=StringIO())
        with pytest.raises(CommandError, match="only apply with --generate"):
            call_command("check_rls", "--name", "x", stdout=StringIO())

    def test_name_must_be_an_identifier(self):
        with pytest.raises(CommandError, match="valid Python identifier"):
            call_command("check_rls", "--generate", "--name", "no-dashes", stdout=StringIO())


class TestHints:
    """A failing run says how to fix each table's issues, from the migrations' point of view."""

    def test_rls_turned_off(self):
        _execute('ALTER TABLE "test_order" DISABLE ROW LEVEL SECURITY')
        code, _, err = _check_rls()
        assert code == 1
        assert "How to fix:" in err
        assert (
            "  Order (test_order): the policy exists, but RLS was turned off outside "
            'migrations. Turn it back on: ALTER TABLE "test_order" ENABLE ROW LEVEL SECURITY;\n'
        ) in err

    def test_policy_dropped(self):
        _execute('DROP POLICY "test_order_tenant_isolation_policy" ON "test_order"')
        _, _, err = _check_rls()
        assert (
            "  Order (test_order): migration test_app.0001_initial created its policy and is "
            "applied on database 'default', so the policy or the table was dropped outside "
            "migrations. `sqlmigrate test_app 0001_initial` prints the DO block"
        ) in err

    def test_a_proxy_does_not_stand_for_its_table(self):
        """The concrete model gets the hint: a proxy's ``Meta`` holds no ``RLSConstraint``."""
        proxy_meta = type("Meta", (), {"app_label": "test_app", "proxy": True})
        with isolate_apps("tests.test_app"):  # the proxy registers in a registry of its own
            proxy = type("OrderProxy", (Order,), {"__module__": __name__, "Meta": proxy_meta})
        _execute('DROP POLICY "test_order_tenant_isolation_policy" ON "test_order"')
        with patch.object(apps, "get_models", return_value=[Order, proxy]):
            assert _collect_rls_models()["test_order"] is Order
            _, _, err = _check_rls()
        assert "  Order (test_order): no RLS policies found" in err
        assert "OrderProxy" not in err
        assert "W014" not in err
        assert "  Order (test_order): migration test_app.0001_initial created its policy" in err

    def test_constraint_migration_not_applied(self):
        _execute(
            'DROP POLICY "test_order_tenant_isolation_policy" ON "test_order"',
            "DELETE FROM django_migrations WHERE app = 'test_app' AND name = '0001_initial'",
        )
        _, _, err = _check_rls()
        assert (
            "  Order (test_order): migration test_app.0001_initial adds its RLSConstraint and "
            "is not applied on database 'default': run migrate."
        ) in err

    def test_m2m_policy_dropped(self):
        _drop_m2m_policies("test_project_tags")
        _, _, err = _check_rls()
        assert (
            f"  Project.tags (auto M2M) (test_project_tags): migration test_app.{_HAND_WRITTEN} "
            f"added its policy and is applied on database 'default', so the policy or the "
            f"table was dropped outside migrations. Run setup_m2m_rls --database default to "
            f"re-create the policy."
        ) in err

    def test_m2m_policy_migration_not_applied(self):
        _drop_m2m_policies("test_project_tags")
        _execute(
            f"DELETE FROM django_migrations WHERE app = 'test_app' AND name = '{_HAND_WRITTEN}'"
        )
        _, _, err = _check_rls()
        assert (
            f"(test_project_tags): migration test_app.{_HAND_WRITTEN} adds its policy and is "
            f"not applied on database 'default': run migrate."
        ) in err

    def test_m2m_policy_no_migration(self, migrations_without_m2m_policies):
        _drop_m2m_policies("test_project_tags")
        _, _, err = _check_rls()
        assert (
            "(test_project_tags): no migration adds its policy (migrate never creates the "
            "policy of an auto-created through table): run check_rls --generate to write "
            "one, then migrate."
        ) in err
        # Without --generate nothing is written.
        assert not (migrations_without_m2m_policies / f"{_GENERATED}.py").exists()

    def test_migrations_that_do_not_load(self):
        _execute('ALTER TABLE "test_order" DISABLE ROW LEVEL SECURITY')
        with patch(
            "django_rls_tenants.management.commands.check_rls.MigrationFacts",
            side_effect=RuntimeError("broken migration graph"),
        ):
            code, _, err = _check_rls()
        assert code == 1
        assert "  Order (test_order): RLS not enabled" in err
        assert (
            "Could not read the migrations to tell how to fix these issues: RuntimeError: "
            "broken migration graph."
        ) in err
        assert "How to fix:" not in err


class _Facts:
    """A stand-in for ``MigrationFacts`` with fixed answers."""

    alias = "default"

    def __init__(
        self,
        *,
        migrated=True,
        has_constraint=True,
        has_field=True,
        constraint_migrations=(),
        m2m_migrations=(),
    ):
        self._migrated = migrated
        self._has_constraint = has_constraint
        self._has_field = has_field
        self._constraint_migrations = list(constraint_migrations)
        self._m2m_migrations = list(m2m_migrations)

    def is_migrated(self, app_label):
        return self._migrated

    def is_applied(self, key):
        return True

    def state_has_rls_constraint(self, model):
        return self._has_constraint

    def state_has_field(self, model, field_name):
        return self._has_field

    def rls_constraint_migrations(self, model):
        return self._constraint_migrations

    def m2m_policy_migrations(self, table):
        return self._m2m_migrations

    def leaf_nodes(self, app_label):
        return [("test_app", _LEAF)]


def _tags_info():
    return _collect_m2m_tables()["test_project_tags"]


class TestHintCauses:
    """The causes the integration tests above cannot set up on the test app."""

    def test_model_without_rls_constraint(self):
        with patch("django_rls_tenants.tenants.models._get_tenant_fk_field", return_value=None):
            hint = model_hint(_Facts(), Order, {NO_POLICIES})
        assert "its Meta does not inherit RLSProtectedModel.Meta (system check W014)" in hint
        assert "makemigrations test_app" in hint

    @pytest.mark.parametrize(
        "facts", [_Facts(has_constraint=False), _Facts(migrated=False)], ids=["state", "app"]
    )
    def test_model_needs_makemigrations(self, facts):
        assert model_hint(facts, Order, {TABLE_MISSING, NO_POLICIES}) == (
            "no migration adds its RLSConstraint: run makemigrations test_app, then migrate."
        )

    def test_model_constraint_in_state_without_an_operation(self):
        hint = model_hint(_Facts(), Order, {NO_POLICIES})
        assert "none of them creates its policy" in hint

    def test_rls_not_enabled_nor_forced(self):
        hint = model_hint(
            _Facts(constraint_migrations=[("test_app", "0001_initial")]),
            Order,
            {RLS_NOT_ENABLED, "RLS not forced"},
        )
        assert hint.endswith(
            'ALTER TABLE "test_order" ENABLE ROW LEVEL SECURITY; '
            'ALTER TABLE "test_order" FORCE ROW LEVEL SECURITY;'
        )

    @pytest.mark.parametrize(
        "facts", [_Facts(has_field=False), _Facts(migrated=False)], ids=["field", "app"]
    )
    def test_m2m_needs_makemigrations(self, facts):
        hint = m2m_hint(facts, "test_project_tags", _tags_info(), {TABLE_MISSING}, {})
        assert hint == (
            "no migration creates Project.tags: run makemigrations test_app and migrate, then "
            "check_rls --generate to write the migration that adds the policy."
        )

    def test_m2m_rls_turned_off(self):
        hint = m2m_hint(
            _Facts(m2m_migrations=[("test_app", _HAND_WRITTEN)]),
            "test_project_tags",
            _tags_info(),
            {RLS_NOT_ENABLED},
            {},
        )
        assert hint.endswith('ALTER TABLE "test_project_tags" ENABLE ROW LEVEL SECURITY;')

    def test_plan_skips_an_app_without_migrations(self):
        planned, skipped = plan_m2m_migrations(
            _Facts(migrated=False),
            {"test_project_tags": _tags_info()},
            {},
            name="m2m_rls_policies",
            conf=RLSTenantsConfig(),
        )
        assert planned == []
        assert skipped == {
            "test_project_tags": (
                "app 'test_app' has no migrations to add it to; run setup_m2m_rls instead."
            )
        }

    def test_plan_skips_a_field_no_migration_creates(self):
        planned, skipped = plan_m2m_migrations(
            _Facts(has_field=False),
            {"test_project_tags": _tags_info()},
            {},
            name="m2m_rls_policies",
            conf=RLSTenantsConfig(),
        )
        assert planned == []
        assert skipped["test_project_tags"] == (
            "no migration creates Project.tags: run makemigrations test_app first."
        )

    def test_plan_numbers_after_the_leaf(self):
        planned, skipped = plan_m2m_migrations(
            _Facts(),
            {"test_project_tags": _tags_info()},
            {},
            name="m2m_rls_policies",
            conf=RLSTenantsConfig(),
        )
        assert skipped == {}
        [migration] = planned
        assert migration.writer.migration.name == _GENERATED
        assert migration.tables == ["test_project_tags"]
        [operation] = migration.writer.migration.operations
        assert operation.from_model == Project._meta.label
        assert operation.to_model == "test_app.Tag"


class TestMigrationFacts:
    """What ``MigrationFacts`` reads from operations the test app's migrations do not use."""

    def test_records_add_constraint_and_separate_database_and_state(self):
        facts = MigrationFacts("default")
        key = ("test_app", "0099_extra")
        operations = [
            migrations.AddConstraint("order", RLSConstraint(field="tenant", name="extra_rls")),
            migrations.SeparateDatabaseAndState(
                database_operations=[
                    AddM2MRLSPolicy(
                        m2m_table="extra_table",
                        from_model="test_app.Project",
                        to_model="test_app.Tag",
                        from_fk="project_id",
                        to_fk="tag_id",
                    )
                ]
            ),
        ]
        for operation in _database_operations(operations):
            facts._record(key, operation)
        assert facts.rls_constraint_migrations(Order) == [("test_app", "0001_initial"), key]
        assert facts.m2m_policy_migrations("extra_table") == [key]

    def test_model_no_migration_creates(self):
        facts = MigrationFacts("default")
        unknown = SimpleNamespace(_meta=SimpleNamespace(label_lower="test_app.nosuchmodel"))
        assert not facts.state_has_rls_constraint(unknown)
        assert not facts.state_has_field(unknown, "tags")
        assert facts.state_has_field(Project, "tags")
