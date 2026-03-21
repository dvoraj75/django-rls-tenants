"""Tests for the setup_m2m_rls management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


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
