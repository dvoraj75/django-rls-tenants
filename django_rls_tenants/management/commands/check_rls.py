"""Management command: check_rls.

Verifies that all RLSProtectedModel subclasses have the expected
RLS policies applied in the database. Reports missing or stale policies.
Also discovers M2M through tables needing RLS coverage.
Supports checking non-default database aliases via ``--database``.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import connections

# Maps ``pg_policy.polcmd`` codes to the SQL command each policy applies to.
_POLCMD_LABELS = {
    "r": "SELECT",
    "a": "INSERT",
    "w": "UPDATE",
    "d": "DELETE",
    "*": "ALL",
}


def _collect_rls_tables() -> dict[str, str]:
    """Return ``{db_table: ModelName}`` for all concrete ``RLSProtectedModel`` subclasses."""
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.tenants.models import RLSProtectedModel  # noqa: PLC0415

    table_to_model: dict[str, str] = {}
    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model._meta.abstract:  # noqa: SLF001
            continue
        table_to_model[model._meta.db_table] = model.__name__  # noqa: SLF001
    return table_to_model


def _collect_m2m_tables() -> dict[str, dict[str, Any]]:
    """Return M2M through table info for auto-generated through tables on RLS-protected models.

    Returns a dict of ``{db_table: info_dict}`` where info_dict contains:
    - ``description``: Human-readable description
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
        "RLS-protected tables and M2M through tables."
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

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        """Check each RLSProtectedModel subclass and M2M through tables."""
        db_alias: str = options["database"]
        quiet: bool = options["quiet"]
        verbose: bool = options["verbose"]
        table_to_model = _collect_rls_tables()
        m2m_tables = _collect_m2m_tables()

        if not table_to_model and not m2m_tables:
            if not quiet:
                self.stdout.write("No RLS-protected models found.")
            return

        errors: list[str] = []

        # Check standard RLS-protected tables
        if table_to_model:
            tables = list(table_to_model.keys())
            self._check_rls_status(tables, table_to_model, errors, db_alias=db_alias)
            self._check_policies(
                tables, table_to_model, errors, db_alias=db_alias, quiet=quiet, verbose=verbose
            )

        # Check M2M through tables
        if m2m_tables:
            if not quiet:
                self.stdout.write("\nM2M through tables:")
            m2m_table_to_desc = {t: info["description"] for t, info in m2m_tables.items()}
            m2m_table_list = list(m2m_table_to_desc.keys())
            self._check_rls_status(m2m_table_list, m2m_table_to_desc, errors, db_alias=db_alias)
            self._check_policies(
                m2m_table_list,
                m2m_table_to_desc,
                errors,
                db_alias=db_alias,
                quiet=quiet,
                verbose=verbose,
            )

        if errors:
            self.stderr.write(self.style.ERROR(f"\nFound {len(errors)} issue(s):"))
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
            raise SystemExit(1)

        if not quiet:
            total = len(table_to_model) + len(m2m_tables)
            self.stdout.write(self.style.SUCCESS(f"\nAll {total} RLS-protected tables verified."))

    def _check_rls_status(
        self,
        tables: list[str],
        table_to_model: dict[str, str],
        errors: list[str],
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
                errors.append(f"  {model_name} ({table}): table does not exist")
                continue
            enabled, forced = rls_status[table]
            if not enabled:
                errors.append(f"  {model_name} ({table}): RLS not enabled")
            if not forced:
                errors.append(f"  {model_name} ({table}): RLS not forced")

    def _check_policies(
        self,
        tables: list[str],
        table_to_model: dict[str, str],
        errors: list[str],
        *,
        db_alias: str = "default",
        quiet: bool = False,
        verbose: bool = False,
    ) -> None:
        """Batch-check RLS policies via ``pg_policies``.

        With ``verbose`` (and not ``quiet``), additionally read each policy's
        live ``USING`` / ``WITH CHECK`` expressions from the ``pg_policy``
        catalog and print them indented under the policy-name line.
        """
        conn = connections[db_alias]
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(tables))
            # Safety: ``placeholders`` contains only literal ``%s`` tokens —
            # no user data is interpolated into the SQL string. Table names
            # are passed as parameterised values, preventing SQL injection.
            cursor.execute(
                f"SELECT tablename, policyname "
                f"FROM pg_policies WHERE tablename IN ({placeholders})",
                tables,
            )
            policies_by_table: dict[str, list[str]] = {}
            for row in cursor.fetchall():
                policies_by_table.setdefault(row[0], []).append(row[1])

            definitions_by_table: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
            if verbose and not quiet:
                # ``pg_policies`` exposes only policy names; the ``pg_policy``
                # catalog holds the live expressions, read back via
                # ``pg_get_expr`` (the definition Postgres actually enforces).
                # Same parameterised-table safety as the query above.
                cursor.execute(
                    f"SELECT c.relname, p.polname, p.polcmd, "
                    f"pg_get_expr(p.polqual, p.polrelid), "
                    f"pg_get_expr(p.polwithcheck, p.polrelid) "
                    f"FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                    f"WHERE c.relname IN ({placeholders})",
                    tables,
                )
                for relname, polname, polcmd, using, with_check in cursor.fetchall():
                    definitions_by_table.setdefault(relname, []).append(
                        (polname, polcmd, using, with_check)
                    )

        for table, model_name in table_to_model.items():
            policies = policies_by_table.get(table, [])
            if not policies:
                errors.append(f"  {model_name} ({table}): no RLS policies found")
            elif not quiet:
                self.stdout.write(f"  {model_name} ({table}): {', '.join(policies)}")
                if verbose:
                    self._write_policy_details(definitions_by_table.get(table, []))

    def _write_policy_details(
        self,
        definitions: list[tuple[str, str, str | None, str | None]],
    ) -> None:
        """Print the indented ``USING`` / ``WITH CHECK`` block for each policy."""
        for polname, polcmd, using, with_check in definitions:
            command = _POLCMD_LABELS.get(polcmd, polcmd)
            self.stdout.write(f"    {polname} [{command}]")
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
