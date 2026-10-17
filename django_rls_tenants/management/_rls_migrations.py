"""What the migrations on disk say about RLS: ``check_rls``'s hints and ``--generate``.

``makemigrations`` writes the policy of a model whose ``Meta`` carries an
``RLSConstraint``. The policy of a model listed in ``RLS_TENANTS["EXTERNAL_MODELS"]``
(a third-party app's) comes from an ``AddRLSPolicy`` operation the project writes
in a migration of its own app (#112). ``makemigrations`` never writes the policy
of an auto-created many-to-many through table either:
``register_m2m_rls()`` adds the ``RLSM2MConstraint`` to the through model at
startup, outside the migration state, so ``migrate`` does not create it. That
policy comes from an ``AddM2MRLSPolicy`` operation, or from ``setup_m2m_rls``,
which reaches one database only (#46).

``MigrationFacts`` reads the migrations and their record on one database.
``model_hint()`` and ``m2m_hint()`` turn a table ``check_rls`` reported into the
fix for its cause, and ``plan_m2m_migrations()`` builds the migrations
``check_rls --generate`` writes: one per app, holding an ``AddM2MRLSPolicy`` for
each through table no migration adds a policy to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from django.db import connections, migrations
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.writer import MigrationWriter

from django_rls_tenants.operations import AddM2MRLSPolicy, AddRLSPolicy
from django_rls_tenants.rls.constraints import RLSConstraint

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ModelState
    from django.db.models import Model

    from django_rls_tenants.tenants.conf import RLSTenantsConfig

# The problems ``check_rls`` reports for a table.
TABLE_MISSING = "table does not exist"
RLS_NOT_ENABLED = "RLS not enabled"
RLS_NOT_FORCED = "RLS not forced"
NO_POLICIES = "no RLS policies found"
# A table listed in EXTERNAL_MODELS whose AddRLSPolicy reads another field than the entry.
FIELD_MISMATCH = "its AddRLSPolicy reads another field than EXTERNAL_MODELS names"

_Key = tuple[str, str]


def _database_operations(operations: Iterable[Operation]) -> Iterator[Operation]:
    """Yield ``operations``, a ``SeparateDatabaseAndState`` replaced by its database side."""
    for operation in operations:
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _database_operations(operation.database_operations)
        else:
            yield operation


def _model_key(model: type[Model]) -> _Key:
    """Return ``(app_label, model_name)``, as the migration state keys its models."""
    app_label, _, model_name = model._meta.label_lower.partition(".")  # noqa: SLF001
    return app_label, model_name


def _label(key: _Key) -> str:
    """Return ``app_label.migration_name`` for a migration key."""
    return f"{key[0]}.{key[1]}"


class MigrationFacts:
    """The migrations on disk, and which of them are applied on database ``alias``."""

    def __init__(self, alias: str) -> None:
        loader = MigrationLoader(connections[alias], ignore_no_migrations=True)
        self.alias = alias
        self._graph = loader.graph
        self._applied: set[_Key] = set(loader.applied_migrations or ())
        self._migrated_apps: set[str] = loader.migrated_apps
        self._state = loader.project_state()
        # m2m_table -> migrations with an AddM2MRLSPolicy for it.
        self._m2m_policies: dict[str, list[_Key]] = {}
        # (app_label, model_name) -> migrations whose database side adds an RLSConstraint.
        self._rls_constraints: dict[_Key, list[_Key]] = {}
        # (app_label, model_name) -> migrations with an AddRLSPolicy for the model.
        self._rls_policies: dict[_Key, list[_Key]] = {}
        # (app_label, model_name) -> the field the latest of those operations reads.
        self._rls_policy_fields: dict[_Key, str] = {}
        for key in self._migration_order():
            for operation in _database_operations(self._graph.nodes[key].operations):
                self._record(key, operation)

    def _migration_order(self) -> list[_Key]:
        """Return every migration in dependency order, so "latest" means what ``migrate`` means.

        The order of the nodes' names (``sorted()``) is that of ``migrate`` only
        inside one app: a project's ``AddRLSPolicy`` for a third-party model lives
        in another app than the model, and a later migration of app ``a`` can
        depend on one of app ``z``. The plan to each leaf (``forwards_plan()``)
        follows the dependencies; independent branches keep the order of the leaves.
        """
        ordered: dict[_Key, None] = {}
        for leaf in sorted(self._graph.leaf_nodes()):
            ordered.update(dict.fromkeys(self._graph.forwards_plan(leaf)))
        return list(ordered)

    def _record(self, key: _Key, operation: Operation) -> None:
        """Note what ``operation`` of migration ``key`` does to RLS, if anything."""
        if isinstance(operation, AddM2MRLSPolicy):
            self._m2m_policies.setdefault(operation.m2m_table, []).append(key)
        elif isinstance(operation, AddRLSPolicy):
            model_key = (operation.app_label or key[0], operation.model_name_lower)
            self._rls_policies.setdefault(model_key, []).append(key)
            self._rls_policy_fields[model_key] = operation.field
        elif isinstance(operation, migrations.CreateModel):
            constraints = operation.options.get("constraints", [])
            if any(isinstance(constraint, RLSConstraint) for constraint in constraints):
                self._rls_constraints.setdefault((key[0], operation.name_lower), []).append(key)
        elif isinstance(operation, migrations.AddConstraint) and isinstance(
            operation.constraint, RLSConstraint
        ):
            model_key = (key[0], operation.model_name_lower)
            self._rls_constraints.setdefault(model_key, []).append(key)

    def is_migrated(self, app_label: str) -> bool:
        """Return whether ``app_label`` has migrations."""
        return app_label in self._migrated_apps

    def is_applied(self, key: _Key) -> bool:
        """Return whether migration ``key`` is applied on the database."""
        return key in self._applied

    def _model_state(self, model: type[Model]) -> ModelState | None:
        """Return the migration state of ``model``, or ``None`` if no migration creates it."""
        state: ModelState | None = self._state.models.get(_model_key(model))
        return state

    def state_has_rls_constraint(self, model: type[Model]) -> bool:
        """Return whether the migrations give ``model`` an ``RLSConstraint``."""
        state = self._model_state(model)
        if state is None:
            return False
        constraints = state.options.get("constraints", [])
        return any(isinstance(constraint, RLSConstraint) for constraint in constraints)

    def state_has_field(self, model: type[Model], field_name: str) -> bool:
        """Return whether the migrations give ``model`` the field ``field_name``."""
        state = self._model_state(model)
        return state is not None and field_name in state.fields

    def rls_constraint_migrations(self, model: type[Model]) -> list[_Key]:
        """Return the migrations whose database side adds an ``RLSConstraint`` to ``model``."""
        return self._rls_constraints.get(_model_key(model), [])

    def rls_policy_migrations(self, model: type[Model]) -> list[_Key]:
        """Return the migrations with an ``AddRLSPolicy`` for ``model``."""
        return self._rls_policies.get(_model_key(model), [])

    def rls_policy_field(self, model: type[Model]) -> str | None:
        """Return the field the latest ``AddRLSPolicy`` for ``model`` reads, or ``None``."""
        return self._rls_policy_fields.get(_model_key(model))

    def rls_policy_models(self) -> list[_Key]:
        """Return ``(app_label, model_name)`` of every model an ``AddRLSPolicy`` targets."""
        return sorted(self._rls_policies)

    def m2m_policy_migrations(self, table: str) -> list[_Key]:
        """Return the migrations with an ``AddM2MRLSPolicy`` for through table ``table``."""
        return self._m2m_policies.get(table, [])

    def leaf_nodes(self, app_label: str) -> list[_Key]:
        """Return the latest migration of ``app_label`` (several when they conflict)."""
        leaves: list[_Key] = self._graph.leaf_nodes(app_label)
        return leaves


def _rls_off_hint(table: str, problems: set[str]) -> str:
    """Return the hint for a table that has its policy but RLS turned off."""
    statements = []
    if RLS_NOT_ENABLED in problems:
        statements.append(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;')
    if RLS_NOT_FORCED in problems:
        statements.append(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;')
    return (
        f"the policy exists, but RLS was turned off outside migrations. Turn it back on: "
        f"{' '.join(statements)}"
    )


def _policy_missing(problems: set[str]) -> bool:
    """Return whether ``problems`` include a missing table or policy, not only RLS turned off."""
    return TABLE_MISSING in problems or NO_POLICIES in problems


def model_hint(facts: MigrationFacts, model: type[Model], problems: set[str]) -> str:
    """Return how to fix the problems ``check_rls`` found on ``model``'s table.

    Args:
        facts: The migrations and their record on the checked database.
        model: The ``RLSProtectedModel`` subclass, or the model listed in
            ``RLS_TENANTS["EXTERNAL_MODELS"]``, whose table has the problems.
        problems: The problems reported for its table (``TABLE_MISSING``, ...).

    Returns:
        The fix for the cause, as one sentence or two.
    """
    from django_rls_tenants.tenants.models import _get_tenant_fk_field  # noqa: PLC0415

    tenant_fk = _get_tenant_fk_field(model)
    constraints = model._meta.constraints  # noqa: SLF001
    if tenant_fk is not None and not any(isinstance(c, RLSConstraint) for c in constraints):
        # Listed in EXTERNAL_MODELS: its policy comes from an AddRLSPolicy migration.
        return _external_model_hint(facts, model, tenant_fk, problems)
    return _protected_model_hint(facts, model, problems, has_constraint=tenant_fk is not None)


def _protected_model_hint(
    facts: MigrationFacts,
    model: type[Model],
    problems: set[str],
    *,
    has_constraint: bool,
) -> str:
    """Return how to fix the problems of an ``RLSProtectedModel`` subclass's table."""
    meta = model._meta  # noqa: SLF001
    app_label = meta.app_label
    if not has_constraint:
        return (
            "the model has no RLSConstraint, because its Meta does not inherit "
            "RLSProtectedModel.Meta (system check W014). Fix the model, then run "
            f"makemigrations {app_label} and migrate."
        )
    if not facts.is_migrated(app_label) or not facts.state_has_rls_constraint(model):
        return (
            f"no migration adds its RLSConstraint: run makemigrations {app_label}, then migrate."
        )
    keys = facts.rls_constraint_migrations(model)
    pending = [key for key in keys if not facts.is_applied(key)]
    if pending:
        return (
            f"migration {_label(pending[0])} adds its RLSConstraint and is not applied on "
            f"database {facts.alias!r}: run migrate."
        )
    if not _policy_missing(problems):
        return _rls_off_hint(meta.db_table, problems)
    if not keys:
        return (
            f"the migrations of {app_label!r} give it an RLSConstraint, but none of them "
            f"creates its policy: check them, and the table, by hand."
        )
    return (
        f"migration {_label(keys[-1])} created its policy and is applied on database "
        f"{facts.alias!r}, so the policy or the table was dropped outside migrations. "
        f"`sqlmigrate {keys[-1][0]} {keys[-1][1]}` prints the DO block that creates the "
        f"policy (it skips a policy that exists): run that block again."
    )


def _external_model_hint(
    facts: MigrationFacts,
    model: type[Model],
    tenant_fk: str,
    problems: set[str],
) -> str:
    """Return how to fix the problems of a model listed in ``EXTERNAL_MODELS``.

    Its policy comes from an ``AddRLSPolicy`` operation in a migration of any app.
    """
    meta = model._meta  # noqa: SLF001
    if FIELD_MISMATCH in problems:
        key = facts.rls_policy_migrations(model)[-1]
        policy_field = facts.rls_policy_field(model)
        return (
            f"the AddRLSPolicy of migration {_label(key)} reads {policy_field!r}, but "
            f"RLS_TENANTS['EXTERNAL_MODELS'] maps {meta.label} to {tenant_fk!r}, so the "
            f"cross-tenant foreign key check and the join filters read another column "
            f"than the policy. Map it to {policy_field!r}; or, if {tenant_fk!r} is the "
            f"tenant field, replace the policy in a new migration: DROP POLICY "
            f'"{meta.db_table}_tenant_isolation_policy" ON "{meta.db_table}", then '
            f"AddRLSPolicy({meta.object_name!r}, app_label={meta.app_label!r}, "
            f"field={tenant_fk!r})."
        )
    if not meta.managed and _policy_missing(problems):
        return (
            "the model is unmanaged (Meta.managed = False), and AddRLSPolicy, like "
            "Django's own operations, does nothing for an unmanaged model: create the "
            "policy by hand."
        )
    keys = facts.rls_policy_migrations(model)
    pending = [key for key in keys if not facts.is_applied(key)]
    if pending:
        return (
            f"migration {_label(pending[0])} adds its policy (AddRLSPolicy) and is not "
            f"applied on database {facts.alias!r}: run migrate."
        )
    if not _policy_missing(problems):
        return _rls_off_hint(meta.db_table, problems)
    if not keys:
        field = "" if tenant_fk == "tenant" else f", field={tenant_fk!r}"
        return (
            f"RLS_TENANTS['EXTERNAL_MODELS'] lists {meta.label}, but no migration has an "
            f"AddRLSPolicy for it. Add AddRLSPolicy({meta.object_name!r}, "
            f"app_label={meta.app_label!r}{field}) to a migration of your own app that "
            f"depends on the {meta.app_label!r} migration creating {tenant_fk!r}, then "
            f"run migrate."
        )
    return (
        f"migration {_label(keys[-1])} created its policy and is applied on database "
        f"{facts.alias!r}, so the policy or the table was dropped outside migrations. "
        f"`sqlmigrate {keys[-1][0]} {keys[-1][1]}` prints the DO block that creates the "
        f"policy (it skips a policy that exists): run that block again."
    )


def m2m_hint(
    facts: MigrationFacts,
    table: str,
    info: dict[str, Any],
    problems: set[str],
    generated: dict[str, str],
) -> str:
    """Return how to fix the problems ``check_rls`` found on an M2M through table.

    Args:
        facts: The migrations and their record on the checked database.
        table: The through table.
        info: Its entry from ``check_rls._collect_m2m_tables()``.
        problems: The problems reported for it (``TABLE_MISSING``, ...).
        generated: ``{table: hint}`` for the tables a migration ``--generate``
            wrote in this run (or printed, with ``--dry-run``) adds a policy to;
            the hint names the migration.

    Returns:
        The fix for the cause, as one sentence or two.
    """
    if table in generated:
        return generated[table]
    keys = facts.m2m_policy_migrations(table)
    pending = [key for key in keys if not facts.is_applied(key)]
    if pending:
        return (
            f"migration {_label(pending[0])} adds its policy and is not applied on "
            f"database {facts.alias!r}: run migrate."
        )
    if not _policy_missing(problems):
        return _rls_off_hint(table, problems)
    if keys:
        return (
            f"migration {_label(keys[-1])} added its policy and is applied on database "
            f"{facts.alias!r}, so the policy or the table was dropped outside migrations. "
            f"Run setup_m2m_rls --database {facts.alias} to re-create the policy."
        )
    model: type[Model] = info["from_model"]
    app_label = model._meta.app_label  # noqa: SLF001
    if not facts.is_migrated(app_label) or not facts.state_has_field(model, info["field"]):
        return (
            f"no migration creates {model.__name__}.{info['field']}: run makemigrations "
            f"{app_label} and migrate, then check_rls --generate to write the migration "
            f"that adds the policy."
        )
    return (
        "no migration adds its policy (migrate never creates the policy of an "
        "auto-created through table): run check_rls --generate to write one, then migrate."
    )


class PlannedMigration(NamedTuple):
    """A migration ``check_rls --generate`` writes."""

    app_label: str
    writer: MigrationWriter
    tables: list[str]


def plan_m2m_migrations(
    facts: MigrationFacts,
    m2m_tables: dict[str, dict[str, Any]],
    policy_names: dict[str, list[str]],
    *,
    name: str,
    conf: RLSTenantsConfig,
) -> tuple[list[PlannedMigration], dict[str, str]]:
    """Plan an ``AddM2MRLSPolicy`` migration per app for the through tables none covers.

    A through table is covered when a migration on disk has an ``AddM2MRLSPolicy``
    for it, applied or not. One that has its policy only in the checked database
    (from ``setup_m2m_rls``) is not, so a fresh database gets it from ``migrate``.
    The operation creates its policy only when the table has no policy of that
    name, so on such a database it changes nothing.

    Args:
        facts: The migrations and their record on the checked database.
        m2m_tables: ``check_rls._collect_m2m_tables()``.
        policy_names: ``{table: [policy name, ...]}`` on the checked database.
        name: The migration name after its number (``"m2m_rls_policies"``).
        conf: The settings the policies read (GUC names, tenant PK type).

    Returns:
        The migrations to write, and ``{table: reason}`` for each uncovered table
        that gets none.

    Raises:
        ValueError: If ``conf`` holds a GUC name or tenant PK type that
            ``AddM2MRLSPolicy`` refuses.
    """
    operations_by_app: dict[str, list[AddM2MRLSPolicy]] = {}
    skipped: dict[str, str] = {}
    for table, info in m2m_tables.items():
        if facts.m2m_policy_migrations(table):
            continue
        from_model: type[Model] = info["from_model"]
        to_model: type[Model] = info["to_model"]
        app_label = from_model._meta.app_label  # noqa: SLF001
        if not facts.is_migrated(app_label):
            skipped[table] = (
                f"app {app_label!r} has no migrations to add it to; run setup_m2m_rls instead."
            )
            continue
        if not facts.state_has_field(from_model, info["field"]):
            skipped[table] = (
                f"no migration creates {from_model.__name__}.{info['field']}: run "
                f"makemigrations {app_label} first."
            )
            continue
        existing = policy_names.get(table, [])
        if existing and f"{table}_m2m_rls_policy" not in existing:
            skipped[table] = (
                f"the table has a policy of another name on database {facts.alias!r} "
                f"({', '.join(existing)}), and AddM2MRLSPolicy would add a second one."
            )
            continue
        operations_by_app.setdefault(app_label, []).append(
            AddM2MRLSPolicy(
                m2m_table=table,
                from_model=from_model._meta.label,  # noqa: SLF001
                to_model=to_model._meta.label,  # noqa: SLF001
                from_fk=info["from_fk"],
                to_fk=info["to_fk"],
                from_tenant_fk=info["from_tenant_fk"],
                to_tenant_fk=info["to_tenant_fk"],
                guc_tenant_var=conf.GUC_CURRENT_TENANT,
                guc_admin_var=conf.GUC_IS_ADMIN,
                tenant_pk_type=conf.TENANT_PK_TYPE,
            )
        )

    planned: list[PlannedMigration] = []
    for app_label, operations in operations_by_app.items():
        tables = [operation.m2m_table for operation in operations]
        leaves = facts.leaf_nodes(app_label)
        if len(leaves) != 1:
            names = ", ".join(_label(leaf) for leaf in sorted(leaves))
            for table in tables:
                skipped[table] = (
                    f"app {app_label!r} has conflicting migrations ({names}): run "
                    f"makemigrations --merge first."
                )
            continue
        leaf = leaves[0]
        number = (MigrationAutodetector.parse_number(leaf[1]) or 0) + 1
        # A subclass, as Django's autodetector builds them: both are class attributes.
        migration_class = type(
            "Migration",
            (migrations.Migration,),
            {"dependencies": [leaf], "operations": list(operations)},
        )
        migration = migration_class(f"{number:04d}_{name}", app_label)
        planned.append(PlannedMigration(app_label, MigrationWriter(migration), tables))
    return planned, skipped
