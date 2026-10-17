"""Management command: check_rls.

Verifies that all RLSProtectedModel subclasses, and the models listed in
``RLS_TENANTS["EXTERNAL_MODELS"]`` (protected by an ``AddRLSPolicy``
migration), have the expected RLS policies applied in the database.
Reports missing or stale policies,
with the fix for each one's cause. Also discovers M2M through tables needing
RLS coverage, and with ``--generate`` writes the ``AddM2MRLSPolicy``
migrations they lack (#46).
Supports checking non-default database aliases via ``--database``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import run_formatters
from django.db import Error, connections

from django_rls_tenants.management._rls_migrations import (
    FIELD_MISMATCH,
    NO_POLICIES,
    RLS_NOT_ENABLED,
    RLS_NOT_FORCED,
    TABLE_MISSING,
    MigrationFacts,
    m2m_hint,
    model_hint,
    plan_m2m_migrations,
)
from django_rls_tenants.tenants.checks import _describe_error
from django_rls_tenants.tenants.conf import RLSTenantsConfig

if TYPE_CHECKING:
    from django.db.models import Model

    from django_rls_tenants.management._rls_migrations import PlannedMigration


_DEFAULT_MIGRATION_NAME = "m2m_rls_policies"


class _Issue(NamedTuple):
    """A problem ``check_rls`` found on one table."""

    table: str
    label: str  # the model name, or the M2M field's description
    problem: str  # TABLE_MISSING, RLS_NOT_ENABLED, RLS_NOT_FORCED or NO_POLICIES

    def __str__(self) -> str:
        return f"  {self.label} ({self.table}): {self.problem}"


def _collect_rls_models() -> dict[str, type[Model]]:
    """Return ``{db_table: model}`` for the concrete RLS-protected models.

    Those are the ``RLSProtectedModel`` subclasses and the models
    ``RLS_TENANTS["EXTERNAL_MODELS"]`` lists. A proxy model shares its concrete
    model's table (and holds no constraints of its own), so the concrete model
    stands for the table.
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.tenants.models import (  # noqa: PLC0415
        RLSProtectedModel,
        _external_model_fields,
    )

    external = _external_model_fields()
    table_to_model: dict[str, type[Model]] = {}
    for model in apps.get_models():
        meta = model._meta  # noqa: SLF001
        if meta.abstract or meta.proxy:
            continue
        if not issubclass(model, RLSProtectedModel) and meta.label_lower not in external:
            continue
        table_to_model[model._meta.db_table] = model  # noqa: SLF001
    return table_to_model


def _relative_path(path: str) -> str:
    """Return ``path`` relative to the working directory when it is below it (as Django does)."""
    try:
        relative = os.path.relpath(path)
    except ValueError:  # another drive on Windows
        return path
    return path if relative.startswith("..") else relative


def _collect_m2m_tables() -> dict[str, dict[str, Any]]:
    """Return M2M through table info for auto-generated through tables on RLS-protected models.

    Returns a dict of ``{db_table: info_dict}`` where info_dict contains:
    - ``description``: Human-readable description
    - ``field``: Name of the ``ManyToManyField`` on the from model
    - ``from_model``: The model defining the M2M field
    - ``to_model``: The related model
    - ``from_table``: db_table of the from model
    - ``to_table``: db_table of the to model
    - ``from_fk``: FK column name for the from side
    - ``to_fk``: FK column name for the to side
    - ``from_tenant_fk``: Tenant FK field on from model (or None)
    - ``to_tenant_fk``: Tenant FK field on to model (or None)
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.models import (  # noqa: PLC0415
        RLSProtectedModel,
        _get_tenant_fk_field,
    )

    m2m_tables: dict[str, dict[str, Any]] = {}
    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model._meta.abstract:  # noqa: SLF001
            continue
        for m2m_field in model._meta.local_many_to_many:  # noqa: SLF001
            through = m2m_field.remote_field.through
            if not through._meta.auto_created:  # type: ignore[union-attr]  # noqa: SLF001
                continue
            table: str = through._meta.db_table  # type: ignore[union-attr]  # noqa: SLF001
            if table in m2m_tables:
                continue  # already discovered from the other side

            to_model_ref = m2m_field.related_model
            if isinstance(to_model_ref, str):
                continue  # unresolved lazy reference

            # Determine tenant FK for each side (consistent with register_m2m_rls)
            from_tenant_fk = _get_tenant_fk_field(model)
            if from_tenant_fk is None:
                from_tenant_fk = rls_tenants_config.TENANT_FK_FIELD

            to_tenant_fk: str | None = None
            if issubclass(to_model_ref, RLSProtectedModel):
                to_tenant_fk = _get_tenant_fk_field(to_model_ref)
                if to_tenant_fk is None:
                    to_tenant_fk = rls_tenants_config.TENANT_FK_FIELD

            desc = f"{model.__name__}.{m2m_field.name} (auto M2M)"
            m2m_tables[table] = {
                "description": desc,
                "field": m2m_field.name,
                "from_model": model,
                "to_model": to_model_ref,
                "from_table": model._meta.db_table,  # noqa: SLF001
                "to_table": to_model_ref._meta.db_table,  # noqa: SLF001
                "from_fk": m2m_field.m2m_column_name(),
                "to_fk": m2m_field.m2m_reverse_name(),
                "from_tenant_fk": from_tenant_fk,
                "to_tenant_fk": to_tenant_fk,
            }

    return m2m_tables


class Command(BaseCommand):
    """Verify RLS policies on all protected tables and M2M through tables."""

    help = (
        "Verify that RLS policies exist and are enabled on all "
        "RLS-protected tables and M2M through tables. With --generate, write the "
        "AddM2MRLSPolicy migrations the M2M through tables lack."
    )

    def add_arguments(self, parser: Any) -> None:
        """Add command-line arguments."""
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to check RLS status on. Default: 'default'.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            default=False,
            help="Suppress success output; only show errors. Useful for CI/CD pipelines.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            default=False,
            help="Show each policy's full definition (USING / WITH CHECK SQL).",
        )
        parser.add_argument(
            "--generate",
            action="store_true",
            default=False,
            help=(
                "Write a migration, in each app, with an AddM2MRLSPolicy operation for "
                "every auto-created M2M through table no migration adds a policy to."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="With --generate, print the migrations instead of writing them.",
        )
        parser.add_argument(
            "--name",
            help=(
                "With --generate, the name of the migrations after their number. "
                f"Default: {_DEFAULT_MIGRATION_NAME!r}."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        """Check each RLSProtectedModel subclass and M2M through tables."""
        db_alias: str = options["database"]
        quiet: bool = options["quiet"]
        verbose: bool = options["verbose"]
        generate: bool = options["generate"]
        dry_run: bool = options["dry_run"]
        name: str | None = options["name"]
        if not generate and (dry_run or name is not None):
            raise CommandError("--dry-run and --name only apply with --generate.")
        if name is not None and not name.isidentifier():
            raise CommandError(
                f"--name must be a valid Python identifier, as Django's migration names "
                f"are; got {name!r}."
            )
        table_to_model = _collect_rls_models()
        m2m_tables = _collect_m2m_tables()

        if not table_to_model and not m2m_tables:
            if not quiet:
                self.stdout.write("No RLS-protected models found.")
            return

        issues: list[_Issue] = []

        try:
            policy_names = self._check_tables(
                {table: model.__name__ for table, model in table_to_model.items()},
                m2m_tables,
                issues,
                db_alias=db_alias,
                quiet=quiet,
                verbose=verbose,
            )
        except Error as exc:
            # A check that could not run must not look like one that passed (#108).
            self.stderr.write(
                self.style.ERROR(
                    f"\nCould not determine the RLS status of the protected tables on "
                    f"database {db_alias!r}: the catalogue query failed with "
                    f"{_describe_error(exc)}. Nothing was verified; fix the cause and run "
                    f"check_rls again."
                )
            )
            raise SystemExit(1) from exc

        facts = self._migration_facts(db_alias, required=generate)
        if facts is not None:
            self._check_external_policies(facts, table_to_model, issues)
        generated: dict[str, str] = {}
        if generate:
            assert facts is not None  # noqa: S101  -- required: MigrationFacts raised otherwise
            generated = self._generate(
                facts,
                m2m_tables,
                policy_names,
                name=name or _DEFAULT_MIGRATION_NAME,
                dry_run=dry_run,
                quiet=quiet,
            )

        if issues:
            self.stderr.write(self.style.ERROR(f"\nFound {len(issues)} issue(s):"))
            for issue in issues:
                self.stderr.write(self.style.ERROR(str(issue)))
            self._write_hints(issues, table_to_model, m2m_tables, db_alias, facts, generated)
            # The database is still unprotected, whatever --generate wrote.
            raise SystemExit(1)

        if not quiet:
            total = len(table_to_model) + len(m2m_tables)
            self.stdout.write(self.style.SUCCESS(f"\nAll {total} RLS-protected tables verified."))

    def _migration_facts(self, db_alias: str, *, required: bool) -> MigrationFacts | None:
        """Read the migrations and their record on ``db_alias``.

        Only ``--generate`` needs them (``required``); otherwise a failure is
        reported and ``None`` returned, since the comparison of ``AddRLSPolicy``
        with ``EXTERNAL_MODELS`` is advice on top of the checks.
        """
        if required:
            return MigrationFacts(db_alias)
        try:
            return MigrationFacts(db_alias)
        except Exception as exc:  # advice; the table checks stand without it
            self.stderr.write(
                f"\nCould not read the migrations to compare their AddRLSPolicy "
                f"operations with RLS_TENANTS['EXTERNAL_MODELS']: {_describe_error(exc)}."
            )
            return None

    def _check_external_policies(
        self,
        facts: MigrationFacts,
        table_to_model: dict[str, type[Model]],
        issues: list[_Issue],
    ) -> None:
        """Compare the ``AddRLSPolicy`` operations on disk with ``EXTERNAL_MODELS``.

        A listed model whose latest ``AddRLSPolicy`` reads another field than its
        entry is an issue (``FIELD_MISMATCH``): the cross-tenant foreign key check
        and the join filters would compare another column than the policy. A model
        an ``AddRLSPolicy`` protects that the setting does not list gets a warning:
        its table is protected, but the library's runtime does not know it.
        """
        from django.apps import apps  # noqa: PLC0415

        from django_rls_tenants.rls.constraints import RLSConstraint  # noqa: PLC0415
        from django_rls_tenants.tenants.models import _external_model_fields  # noqa: PLC0415

        external = _external_model_fields()
        for table, model in table_to_model.items():
            meta = model._meta  # noqa: SLF001
            entry = external.get(meta.label_lower)
            if entry is None or any(isinstance(c, RLSConstraint) for c in meta.constraints):
                continue  # a constraint's policy: the operation does nothing for it
            policy_field = facts.rls_policy_field(model)
            if policy_field is not None and policy_field != entry:
                issues.append(_Issue(table, model.__name__, FIELD_MISMATCH))
        for app_label, model_name in facts.rls_policy_models():
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                continue  # the model was removed after its migration
            meta = model._meta  # noqa: SLF001
            if meta.label_lower in external or meta.proxy:
                continue
            if any(isinstance(c, RLSConstraint) for c in meta.constraints):
                continue
            key = facts.rls_policy_migrations(model)[-1]
            field = facts.rls_policy_field(model)
            self.stderr.write(
                self.style.WARNING(
                    f"{meta.label} ({meta.db_table}): migration {key[0]}.{key[1]} has an "
                    f"AddRLSPolicy for it, but RLS_TENANTS['EXTERNAL_MODELS'] does not "
                    f"list it, so foreign keys to it are not checked against the tenant "
                    f"and joins into it get no tenant filter. Add {meta.label!r}: "
                    f"{field!r} to RLS_TENANTS['EXTERNAL_MODELS']."
                )
            )

    def _check_tables(
        self,
        table_to_model: dict[str, str],
        m2m_tables: dict[str, dict[str, Any]],
        issues: list[_Issue],
        *,
        db_alias: str,
        quiet: bool,
        verbose: bool,
    ) -> dict[str, list[str]]:
        """Check the RLS-protected tables, then the M2M through tables, on ``db_alias``.

        Returns:
            ``{table: [policy name, ...]}`` for the tables that have policies.
        """
        policy_names: dict[str, list[str]] = {}
        if table_to_model:
            tables = list(table_to_model.keys())
            self._check_rls_status(tables, table_to_model, issues, db_alias=db_alias)
            policy_names |= self._check_policies(
                tables, table_to_model, issues, db_alias=db_alias, quiet=quiet, verbose=verbose
            )

        if m2m_tables:
            if not quiet:
                self.stdout.write("\nM2M through tables:")
            m2m_table_to_desc = {t: info["description"] for t, info in m2m_tables.items()}
            m2m_table_list = list(m2m_table_to_desc.keys())
            self._check_rls_status(m2m_table_list, m2m_table_to_desc, issues, db_alias=db_alias)
            policy_names |= self._check_policies(
                m2m_table_list,
                m2m_table_to_desc,
                issues,
                db_alias=db_alias,
                quiet=quiet,
                verbose=verbose,
            )
        return policy_names

    def _check_rls_status(
        self,
        tables: list[str],
        table_to_model: dict[str, str],
        issues: list[_Issue],
        *,
        db_alias: str = "default",
    ) -> None:
        """Batch-check ``relrowsecurity`` / ``relforcerowsecurity`` via ``pg_class``."""
        conn = connections[db_alias]
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(tables))
            # Safety: ``placeholders`` contains only literal ``%s`` tokens —
            # no user data is interpolated into the SQL string. Table names
            # are passed as parameterised values, preventing SQL injection.
            cursor.execute(
                f"SELECT relname, relrowsecurity, relforcerowsecurity "
                f"FROM pg_class WHERE relname IN ({placeholders})",
                tables,
            )
            rls_status = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

        for table, model_name in table_to_model.items():
            if table not in rls_status:
                issues.append(_Issue(table, model_name, TABLE_MISSING))
                continue
            enabled, forced = rls_status[table]
            if not enabled:
                issues.append(_Issue(table, model_name, RLS_NOT_ENABLED))
            if not forced:
                issues.append(_Issue(table, model_name, RLS_NOT_FORCED))

    def _check_policies(
        self,
        tables: list[str],
        table_to_model: dict[str, str],
        issues: list[_Issue],
        *,
        db_alias: str = "default",
        quiet: bool = False,
        verbose: bool = False,
    ) -> dict[str, list[str]]:
        """Batch-check RLS policies via ``pg_policies``.

        With ``verbose`` (and not ``quiet``), additionally print each policy's
        live ``USING`` / ``WITH CHECK`` expressions indented under the
        policy-name line, taken from the ``pg_policies`` view's ``qual`` /
        ``with_check`` columns.

        Returns:
            ``{table: [policy name, ...]}`` for the tables that have policies.
        """
        conn = connections[db_alias]
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(tables))
            # Safety: ``placeholders`` contains only literal ``%s`` tokens —
            # no user data is interpolated into the SQL string. Table names
            # are passed as parameterised values, preventing SQL injection.
            #
            # ``cmd``/``qual``/``with_check`` come straight from ``pg_policies``:
            # the view already decodes ``polcmd`` to its command word and runs
            # ``pg_get_expr`` over the live policy nodes (the definition Postgres
            # enforces), so verbose mode needs no extra catalog round-trip.
            cursor.execute(
                f"SELECT tablename, policyname, cmd, qual, with_check "
                f"FROM pg_policies WHERE tablename IN ({placeholders})",
                tables,
            )
            policies_by_table: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
            for tablename, policyname, cmd, qual, with_check in cursor.fetchall():
                policies_by_table.setdefault(tablename, []).append(
                    (policyname, cmd, qual, with_check)
                )

        for table, model_name in table_to_model.items():
            policies = policies_by_table.get(table, [])
            if not policies:
                issues.append(_Issue(table, model_name, NO_POLICIES))
            elif not quiet:
                names = ", ".join(policy[0] for policy in policies)
                self.stdout.write(f"  {model_name} ({table}): {names}")
                if verbose:
                    self._write_policy_details(policies)
        return {
            table: [policy[0] for policy in policies]
            for table, policies in policies_by_table.items()
        }

    def _generate(
        self,
        facts: MigrationFacts,
        m2m_tables: dict[str, dict[str, Any]],
        policy_names: dict[str, list[str]],
        *,
        name: str,
        dry_run: bool,
        quiet: bool,
    ) -> dict[str, str]:
        """Write (or print) the ``AddM2MRLSPolicy`` migrations the through tables lack.

        Returns:
            ``{table: hint}`` for each through table a migration was planned for,
            naming the migration, for ``_write_hints()``.
        """
        try:
            planned, skipped = plan_m2m_migrations(
                facts, m2m_tables, policy_names, name=name, conf=RLSTenantsConfig()
            )
        except ValueError as exc:  # a GUC name or tenant PK type the operation refuses
            raise CommandError(str(exc)) from exc

        generated: dict[str, str] = {}
        written: list[str] = []
        for migration in planned:
            written.extend(self._write_migration(migration, dry_run=dry_run))
            label = f"{migration.app_label}.{migration.writer.migration.name}"
            hint = (
                f"check_rls --generate --dry-run printed migration {label}, which adds "
                f"its policy: run check_rls --generate to write it, then migrate."
                if dry_run
                else f"check_rls --generate wrote migration {label}, which adds its "
                f"policy: run migrate."
            )
            generated.update(dict.fromkeys(migration.tables, hint))
        if written:
            run_formatters(written, stderr=self.stderr)
        for table, reason in skipped.items():
            self.stderr.write(
                self.style.WARNING(
                    f"No migration generated for {m2m_tables[table]['description']} "
                    f"({table}): {reason}"
                )
            )
        if not planned and not skipped and not quiet:
            self.stdout.write(
                "\nNo migration to generate: a migration adds the policy of every "
                "auto-created M2M through table."
            )
        return generated

    def _write_migration(self, migration: PlannedMigration, *, dry_run: bool) -> list[str]:
        """Describe ``migration`` and write it, or print it with ``dry_run``.

        Returns:
            The path written, or nothing with ``dry_run``.
        """
        writer = migration.writer
        path = _relative_path(writer.path)
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nMigrations for {migration.app_label!r}:"))
        self.stdout.write(f"  {self.style.MIGRATE_LABEL(path)}")
        for operation in writer.migration.operations:
            self.stdout.write(f"    {operation.formatted_description()}")
        if dry_run:
            self.stdout.write(writer.as_string())
            return []
        try:
            with Path(writer.path).open("x", encoding="utf-8") as migration_file:
                migration_file.write(writer.as_string())
        except FileExistsError as exc:
            raise CommandError(
                f"{path} exists already and is not a migration Django loads. Move it "
                f"away, or pass --name to write the migration under another name."
            ) from exc
        return [writer.path]

    def _write_hints(
        self,
        issues: list[_Issue],
        table_to_model: dict[str, type[Model]],
        m2m_tables: dict[str, dict[str, Any]],
        db_alias: str,
        facts: MigrationFacts | None,
        generated: dict[str, str],
    ) -> None:
        """Print the fix for the cause of each table's issues, read from the migrations."""
        if facts is None:
            try:
                facts = MigrationFacts(db_alias)
            except Exception as exc:  # the hints are advice; the issues stand without them
                self.stderr.write(
                    f"\nCould not read the migrations to tell how to fix these issues: "
                    f"{_describe_error(exc)}."
                )
                return
        problems_by_table: dict[str, set[str]] = {}
        labels: dict[str, str] = {}
        for issue in issues:
            problems_by_table.setdefault(issue.table, set()).add(issue.problem)
            labels.setdefault(issue.table, issue.label)
        self.stderr.write("\nHow to fix:")
        for table, problems in problems_by_table.items():
            if table in m2m_tables:
                hint = m2m_hint(facts, table, m2m_tables[table], problems, generated)
            else:
                hint = model_hint(facts, table_to_model[table], problems)
            self.stderr.write(f"  {labels[table]} ({table}): {hint}")

    def _write_policy_details(
        self,
        policies: list[tuple[str, str, str | None, str | None]],
    ) -> None:
        """Print the indented ``USING`` / ``WITH CHECK`` block for each policy.

        Each tuple is ``(policyname, cmd, using, with_check)`` straight from the
        ``pg_policies`` view, where ``cmd`` is already the command word
        (``ALL`` / ``SELECT`` / ...).
        """
        for polname, cmd, using, with_check in policies:
            self.stdout.write(f"    {polname} [{cmd}]")
            self._write_clause("USING", using)
            self._write_clause("WITH CHECK", with_check)

    def _write_clause(self, label: str, expr: str | None) -> None:
        """Print one policy clause, indenting each line of its expression.

        ``pg_get_expr`` pretty-prints non-trivial expressions across several
        lines, so the label goes on its own line and the expression is indented
        beneath it (preserving Postgres's own formatting) rather than appended
        inline. A ``NULL`` expression (e.g. an INSERT policy has no ``USING``)
        is skipped.
        """
        if expr is None:
            return
        self.stdout.write(f"      {label}:")
        # ``pg_get_expr`` returns a leading newline before the expression;
        # strip it so the block starts cleanly, and avoid emitting trailing
        # whitespace on any blank lines.
        for line in expr.strip().splitlines():
            self.stdout.write(f"        {line}" if line else "")
