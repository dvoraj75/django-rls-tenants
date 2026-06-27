"""Tests for the setup_m2m_rls management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from django_rls_tenants.management.commands.check_rls import _collect_m2m_tables
from django_rls_tenants.rls.constraints import _build_m2m_drop_sql

pytestmark = pytest.mark.django_db


def _unprotect_one_m2m_table() -> str:
    """Drop the RLS policy on one discovered M2M table and return its name.

    Integration tests run under ``SET ROLE`` (the autouse ``enforce_rls``
    fixture switches to a non-superuser), but dropping and re-creating
    policies needs table ownership, so we ``RESET ROLE`` back to the
    superuser login role first. Every change here is DDL inside the test's
    transaction, so it is rolled back automatically once the test finishes.

    Leaving the table unprotected forces ``setup_m2m_rls`` to actually apply
    a policy -- otherwise every M2M table is already protected and the
    command only ever reports "skipping", never exercising the apply path
    that ``--verbose`` annotates.
    """
    table = next(iter(_collect_m2m_tables()))
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(_build_m2m_drop_sql(table=table))
    return table


def _m2m_policy_exists(table: str) -> bool:
    """Return whether the M2M RLS policy exists on ``table``.

    Queries ``pg_policies`` directly so tests can assert on the real database
    state -- e.g. that ``--dry-run`` did *not* create the policy -- instead of
    trusting the command's own stdout.
    """
    policy_name = f"{table}_m2m_rls_policy"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_policies WHERE tablename = %s AND policyname = %s",
            [table, policy_name],
        )
        return cursor.fetchone() is not None


class TestSetupM2MRlsCommand:
    """Tests for the setup_m2m_rls management command."""

    def test_reports_already_protected_tables(self):
        """Tables with existing policies are reported as skipped."""
        out = StringIO()
        call_command("setup_m2m_rls", stdout=out)
        output = out.getvalue()
        assert "skipping" in output.lower() or "skipped" in output.lower()

    def test_dry_run_mode(self):
        """Dry run shows SQL without executing."""
        out = StringIO()
        call_command("setup_m2m_rls", "--dry-run", stdout=out)
        output = out.getvalue()
        assert "dry run" in output.lower() or "Dry run" in output

    def test_skips_already_protected(self):
        """Command skips tables that already have policies."""
        out = StringIO()
        call_command("setup_m2m_rls", stdout=out)
        output = out.getvalue()
        # All M2M tables from the migration should already have policies
        assert "policy applied" not in output.lower() or "skipping" in output.lower()

    def test_verbose_prints_sql_and_applies(self):
        """--verbose prints each policy's SQL and still applies it."""
        table = _unprotect_one_m2m_table()
        assert not _m2m_policy_exists(table)  # precondition: dropped above

        out = StringIO()
        call_command("setup_m2m_rls", "--verbose", stdout=out)
        output = out.getvalue()

        # SQL is printed before execution...
        assert "CREATE POLICY" in output
        assert f"{table}_m2m_rls_policy" in output
        # ...and the policy is actually applied (unlike --dry-run).
        assert "policy applied" in output
        assert _m2m_policy_exists(table)

    def test_non_verbose_does_not_print_sql(self):
        """Without --verbose the policy is applied but the SQL is not printed."""
        table = _unprotect_one_m2m_table()

        out = StringIO()
        call_command("setup_m2m_rls", stdout=out)
        output = out.getvalue()

        assert "policy applied" in output
        assert "CREATE POLICY" not in output
        assert _m2m_policy_exists(table)

    def test_dry_run_does_not_apply_unprotected_table(self):
        """--dry-run prints the SQL for an unprotected table but never applies it."""
        table = _unprotect_one_m2m_table()
        assert not _m2m_policy_exists(table)  # precondition: dropped above

        out = StringIO()
        call_command("setup_m2m_rls", "--dry-run", stdout=out)
        output = out.getvalue()

        # SQL is shown and counted as "would be updated"...
        assert "CREATE POLICY" in output
        assert f"{table}_m2m_rls_policy" in output
        assert "would be updated" in output
        # ...but nothing is executed: no success line and no policy in the catalog.
        assert "policy applied" not in output
        assert not _m2m_policy_exists(table)

    def test_verbose_dry_run_prints_once_without_applying(self):
        """--verbose --dry-run prints the SQL exactly once and applies nothing."""
        table = _unprotect_one_m2m_table()

        out = StringIO()
        call_command("setup_m2m_rls", "--verbose", "--dry-run", stdout=out)
        output = out.getvalue()

        # dry-run wins: the SQL is printed a single time (the verbose and
        # dry-run branches share one block, so it is not duplicated)...
        assert output.count("CREATE POLICY") == 1
        # ...and the policy is never created.
        assert "policy applied" not in output
        assert not _m2m_policy_exists(table)

    @override_settings(
        RLS_TENANTS={
            "TENANT_MODEL": "test_app.Tenant",
            "GUC_PREFIX": "myco",
            "TENANT_FK_FIELD": "tenant",
            "TENANT_PK_TYPE": "int",
        }
    )
    def test_guc_names_derived_from_settings(self):
        """GUC names come from RLS_TENANTS, not hardcoded rls.* / int (#57).

        Pre-#57 the command hardcoded ``rls.is_admin`` / ``rls.current_tenant`` /
        ``int``; with ``GUC_PREFIX='myco'`` the emitted SQL must use the
        ``myco.*`` names, and each read must be InitPlan-wrapped.
        """
        _unprotect_one_m2m_table()  # force the apply/print path

        out = StringIO()
        call_command("setup_m2m_rls", "--dry-run", stdout=out)
        output = out.getvalue()

        # GUC names honour the configured prefix, InitPlan-wrapped...
        assert "(SELECT current_setting('myco.is_admin', true)) = 'true'" in output
        assert "(SELECT current_setting('myco.current_tenant', true))" in output
        # ...and the previously hardcoded rls.* GUC names are gone.
        assert "rls.is_admin" not in output
        assert "rls.current_tenant" not in output
        # InitPlan form, not the pre-#57 inline read.
        assert "nullif(current_setting(" not in output

    @override_settings(
        RLS_TENANTS={
            "TENANT_MODEL": "test_app.Tenant",
            "TENANT_PK_TYPE": "text",  # not in the {int, bigint, uuid} allowlist
        }
    )
    def test_invalid_tenant_pk_type_rejected(self):
        """A bad TENANT_PK_TYPE is rejected before it reaches raw DDL (#57).

        The command interpolates ``TENANT_PK_TYPE`` into a ``::<type>`` cast and
        reads it straight from settings, so an out-of-allowlist value must raise
        a clean ``CommandError`` instead of emitting invalid policy SQL.
        """
        with pytest.raises(CommandError, match="Invalid tenant_pk_type"):
            call_command("setup_m2m_rls", "--dry-run", stdout=StringIO())

    @override_settings(
        RLS_TENANTS={
            "TENANT_MODEL": "test_app.Tenant",
            "GUC_PREFIX": "bad prefix",  # space is not a valid GUC-name character
        }
    )
    def test_invalid_guc_prefix_rejected(self):
        """A malformed GUC_PREFIX is rejected before it reaches raw DDL (#57)."""
        with pytest.raises(CommandError, match="Invalid GUC name"):
            call_command("setup_m2m_rls", "--dry-run", stdout=StringIO())
