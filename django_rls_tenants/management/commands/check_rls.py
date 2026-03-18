"""Management command: check_rls.

Verifies that all RLSProtectedModel subclasses have the expected
RLS policies applied in the database. Reports missing or stale policies.
Supports checking non-default database aliases via ``--database``.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import connections


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


class Command(BaseCommand):
    """Verify RLS policies on all protected tables."""

    help = "Verify that RLS policies exist and are enabled on all RLS-protected tables."

    def add_arguments(self, parser: Any) -> None:
        """Add command-line arguments."""
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to check RLS status on. Default: 'default'.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        """Check each RLSProtectedModel subclass."""
        db_alias: str = options["database"]
        table_to_model = _collect_rls_tables()
        if not table_to_model:
            self.stdout.write("No RLS-protected models found.")
            return

        tables = list(table_to_model.keys())
        errors: list[str] = []

        self._check_rls_status(tables, table_to_model, errors, db_alias=db_alias)
        self._check_policies(tables, table_to_model, errors, db_alias=db_alias)

        if errors:
            self.stderr.write(self.style.ERROR(f"\nFound {len(errors)} issue(s):"))
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
            raise SystemExit(1)

        self.stdout.write(
            self.style.SUCCESS(f"\nAll {len(table_to_model)} RLS-protected tables verified.")
        )

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
    ) -> None:
        """Batch-check RLS policies via ``pg_policies``."""
        conn = connections[db_alias]
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(tables))
            cursor.execute(
                f"SELECT tablename, policyname "
                f"FROM pg_policies WHERE tablename IN ({placeholders})",
                tables,
            )
            policies_by_table: dict[str, list[str]] = {}
            for row in cursor.fetchall():
                policies_by_table.setdefault(row[0], []).append(row[1])

        for table, model_name in table_to_model.items():
            policies = policies_by_table.get(table, [])
            if not policies:
                errors.append(f"  {model_name} ({table}): no RLS policies found")
            else:
                self.stdout.write(f"  {model_name} ({table}): {', '.join(policies)}")
