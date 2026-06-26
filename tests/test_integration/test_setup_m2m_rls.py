"""Tests for the setup_m2m_rls management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

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

        out = StringIO()
        call_command("setup_m2m_rls", "--verbose", stdout=out)
        output = out.getvalue()

        # SQL is printed before execution...
        assert "CREATE POLICY" in output
        assert f"{table}_m2m_rls_policy" in output
        # ...and the policy is actually applied (unlike --dry-run).
        assert "policy applied" in output

    def test_non_verbose_does_not_print_sql(self):
        """Without --verbose the policy is applied but the SQL is not printed."""
        _unprotect_one_m2m_table()

        out = StringIO()
        call_command("setup_m2m_rls", stdout=out)
        output = out.getvalue()

        assert "policy applied" in output
        assert "CREATE POLICY" not in output
