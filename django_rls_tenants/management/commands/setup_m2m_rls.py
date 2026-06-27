"""Management command: setup_m2m_rls.

Discovers M2M through tables that need RLS policies and applies them.
Designed for existing deployments that need to add M2M RLS coverage
without re-running migrations.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from django_rls_tenants.management.commands.check_rls import _collect_m2m_tables
from django_rls_tenants.rls.constraints import (
    _build_m2m_conditions,
    _build_m2m_create_sql,
    _validate_guc_name_for_ddl,
    _validate_pk_type,
)
from django_rls_tenants.rls.policy_sql import bool_flag_sql
from django_rls_tenants.tenants.conf import RLSTenantsConfig


class Command(BaseCommand):
    """Apply RLS policies to unprotected M2M through tables."""

    help = (
        "Discover M2M through tables on RLS-protected models "
        "and apply subquery-based RLS policies."
    )

    def add_arguments(self, parser: Any) -> None:
        """Add command-line arguments."""
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to apply policies on. Default: 'default'.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print the SQL that would be executed without applying it.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            default=False,
            help="Print each policy's SQL before applying it.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        """Discover and apply M2M RLS policies."""
        db_alias: str = options["database"]
        dry_run: bool = options["dry_run"]
        verbose: bool = options["verbose"]

        m2m_info = _collect_m2m_tables()
        if not m2m_info:
            self.stdout.write("No M2M through tables found on RLS-protected models.")
            return

        # Derive GUC names and the tenant PK cast from the live RLS_TENANTS
        # settings instead of hardcoding "rls.*"/"int", so a custom GUC_PREFIX
        # or TENANT_PK_TYPE is honoured. A fresh reader (not the cached
        # module-level singleton) reflects settings overridden after startup.
        conf = RLSTenantsConfig()

        # These values flow straight into raw CREATE POLICY DDL. RLSConstraint
        # and AddM2MRLSPolicy validate the equivalent arguments at construction;
        # this command reads them from settings, so guard here too (a malformed
        # GUC_PREFIX or TENANT_PK_TYPE otherwise surfaces as a cryptic SQL error).
        try:
            _validate_pk_type(conf.TENANT_PK_TYPE)
            _validate_guc_name_for_ddl(conf.GUC_CURRENT_TENANT, "RLS_TENANTS['GUC_PREFIX']")
            _validate_guc_name_for_ddl(conf.GUC_IS_ADMIN, "RLS_TENANTS['GUC_PREFIX']")
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        conn = connections[db_alias]

        # Check which tables already have policies
        tables = list(m2m_info.keys())
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(tables))
            cursor.execute(
                f"SELECT tablename, policyname "
                f"FROM pg_policies WHERE tablename IN ({placeholders})",
                tables,
            )
            existing_policies: dict[str, list[str]] = {}
            for row in cursor.fetchall():
                existing_policies.setdefault(row[0], []).append(row[1])

        applied = 0
        skipped = 0

        for table, info in m2m_info.items():
            policies = existing_policies.get(table, [])
            if policies:
                self.stdout.write(
                    f"  {info['description']} ({table}): "
                    f"already has policies: {', '.join(policies)} -- skipping"
                )
                skipped += 1
                continue

            admin_check = bool_flag_sql(conf.GUC_IS_ADMIN)
            subquery_clause = _build_m2m_conditions(
                from_fk=info["from_fk"],
                from_table=info["from_table"],
                from_tenant_fk=info["from_tenant_fk"],
                to_fk=info["to_fk"],
                to_table=info["to_table"],
                to_tenant_fk=info["to_tenant_fk"],
                guc_tenant_var=conf.GUC_CURRENT_TENANT,
                tenant_pk_type=conf.TENANT_PK_TYPE,
            )
            sql = _build_m2m_create_sql(
                table=table, admin_check=admin_check, subquery_clause=subquery_clause
            )

            if verbose or dry_run:
                self.stdout.write(f"\n-- {info['description']} ({table}):")
                self.stdout.write(sql)
            if not dry_run:
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                self.stdout.write(
                    self.style.SUCCESS(f"  {info['description']} ({table}): policy applied")
                )
            applied += 1

        if dry_run:
            self.stdout.write(
                f"\nDry run: {applied} table(s) would be updated, {skipped} skipped."
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\nApplied policies to {applied} table(s), {skipped} skipped.")
            )
