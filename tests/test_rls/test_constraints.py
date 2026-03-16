"""Tests for django_rls_tenants.rls.constraints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from django_rls_tenants.rls.constraints import RLSConstraint


def _make_mock_model(db_table: str = "test_order") -> MagicMock:
    """Create a mock model with _meta.db_table set."""
    model = MagicMock()
    model._meta.db_table = db_table
    return model


def _get_create_sql(constraint: RLSConstraint, db_table: str = "test_order") -> str:
    """Get the SQL string from create_sql()."""
    model = _make_mock_model(db_table)
    statement = constraint.create_sql(model, schema_editor=MagicMock())
    return str(statement)


def _get_remove_sql(constraint: RLSConstraint, db_table: str = "test_order") -> str:
    """Get the SQL string from remove_sql()."""
    model = _make_mock_model(db_table)
    statement = constraint.remove_sql(model, schema_editor=MagicMock())
    return str(statement)


class TestCreateSQL:
    """Tests for RLSConstraint.create_sql()."""

    def test_generates_enable_rls(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "ENABLE ROW LEVEL SECURITY" in sql

    def test_generates_force_rls(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "FORCE ROW LEVEL SECURITY" in sql

    def test_generates_policy_with_using_and_check(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "CREATE POLICY" in sql
        assert "USING" in sql
        assert "WITH CHECK" in sql

    def test_policy_name_derived_from_table(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c, db_table="my_orders")
        assert "my_orders_tenant_isolation_policy" in sql

    def test_extra_bypass_flags_in_using_only(self):
        c = RLSConstraint(
            field="tenant",
            name="test_rls",
            extra_bypass_flags=["rls.is_login_request"],
        )
        sql = _get_create_sql(c)
        # The bypass flag should appear in the USING clause
        assert "rls.is_login_request" in sql
        # Split at WITH CHECK to verify it's NOT in the WITH CHECK clause
        parts = sql.split("WITH CHECK")
        assert len(parts) == 2
        assert "rls.is_login_request" in parts[0]  # in USING
        assert "rls.is_login_request" not in parts[1]  # NOT in WITH CHECK

    def test_idempotent_if_not_exists(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "IF NOT EXISTS" in sql

    def test_schema_check_in_idempotency(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "schemaname = current_schema()" in sql

    def test_tenant_pk_type_int(self):
        c = RLSConstraint(field="tenant", name="test_rls", tenant_pk_type="int")
        sql = _get_create_sql(c)
        assert "::int" in sql

    def test_tenant_pk_type_uuid(self):
        c = RLSConstraint(field="tenant", name="test_rls", tenant_pk_type="uuid")
        sql = _get_create_sql(c)
        assert "::uuid" in sql

    def test_tenant_field_id_in_policy(self):
        c = RLSConstraint(field="organization", name="test_rls")
        sql = _get_create_sql(c)
        assert "organization_id" in sql

    def test_uses_case_when_structure(self):
        """Policy uses CASE WHEN for index-friendly constant folding."""
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "CASE WHEN" in sql
        assert "THEN true" in sql
        assert "ELSE tenant_id" in sql
        assert "END" in sql

    def test_no_top_level_or_in_policy_predicate(self):
        """USING/WITH CHECK clauses have no top-level OR (it's inside CASE WHEN)."""
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        # Split out the USING and WITH CHECK clause contents
        parts = sql.split("WITH CHECK")
        using_part = parts[0].split("USING")[1]
        check_part = parts[1]
        # The OR should NOT appear at the top level of USING or WITH CHECK.
        # It may appear inside the CASE WHEN condition (for extra bypass flags),
        # but the basic case has no OR at all.
        # Verify the old pattern "tenant_match OR admin_bypass" is gone.
        assert "OR coalesce" not in using_part
        assert "OR coalesce" not in check_part

    def test_no_redundant_coalesce_null(self):
        """Policy does not contain the redundant coalesce(..., NULL) wrapper."""
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_create_sql(c)
        assert "coalesce" not in sql.lower()

    def test_extra_bypass_flags_use_case_when_with_or(self):
        """Extra bypass flags are OR'd inside the CASE WHEN condition."""
        c = RLSConstraint(
            field="tenant",
            name="test_rls",
            extra_bypass_flags=["rls.is_login_request", "rls.is_preauth_request"],
        )
        sql = _get_create_sql(c)
        # Both flags should appear in the CASE WHEN condition (USING only)
        parts = sql.split("WITH CHECK")
        using_part = parts[0]
        check_part = parts[1]
        assert "rls.is_login_request" in using_part
        assert "rls.is_preauth_request" in using_part
        assert "rls.is_login_request" not in check_part
        assert "rls.is_preauth_request" not in check_part
        # The CASE WHEN in USING should contain OR for the bypass flags
        assert "CASE WHEN" in using_part


class TestRemoveSQL:
    """Tests for RLSConstraint.remove_sql()."""

    def test_drops_policy(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_remove_sql(c)
        assert "DROP POLICY IF EXISTS" in sql

    def test_no_force_rls(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_remove_sql(c)
        assert "NO FORCE ROW LEVEL SECURITY" in sql

    def test_disables_rls(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        sql = _get_remove_sql(c)
        assert "DISABLE ROW LEVEL SECURITY" in sql


class TestDeconstruct:
    """Tests for RLSConstraint.deconstruct()."""

    def test_roundtrip(self):
        c = RLSConstraint(
            field="tenant",
            name="test_rls",
            guc_tenant_var="app.tenant",
            tenant_pk_type="uuid",
            extra_bypass_flags=["rls.is_login"],
        )
        _path, _args, kwargs = c.deconstruct()
        reconstructed = RLSConstraint(**kwargs)
        assert c == reconstructed

    def test_defaults_omitted(self):
        c = RLSConstraint(field="tenant", name="test_rls")
        _, _, kwargs = c.deconstruct()
        assert "field" in kwargs
        assert "guc_tenant_var" not in kwargs
        assert "guc_admin_var" not in kwargs
        assert "tenant_pk_type" not in kwargs
        assert "extra_bypass_flags" not in kwargs


class TestEquality:
    """Tests for __eq__ and __hash__."""

    def test_eq_same_params(self):
        a = RLSConstraint(field="tenant", name="rls")
        b = RLSConstraint(field="tenant", name="rls")
        assert a == b

    def test_eq_different_params(self):
        a = RLSConstraint(field="tenant", name="rls")
        b = RLSConstraint(field="org", name="rls")
        assert a != b

    def test_hash_same_params(self):
        a = RLSConstraint(field="tenant", name="rls")
        b = RLSConstraint(field="tenant", name="rls")
        assert hash(a) == hash(b)

    def test_hash_different_params(self):
        a = RLSConstraint(field="tenant", name="rls")
        b = RLSConstraint(field="tenant", name="other")
        assert hash(a) != hash(b)


class TestInputValidation:
    """Tests for SQL injection prevention via input validation."""

    def test_invalid_field_name_semicolon(self):
        """Field name with semicolon is rejected."""
        with pytest.raises(ValueError, match="Invalid field name"):
            RLSConstraint(field="field; DROP", name="test_rls")

    def test_invalid_field_name_dotted(self):
        """Dotted field name is rejected (dots not valid for column names)."""
        with pytest.raises(ValueError, match="Invalid field name"):
            RLSConstraint(field="a.b", name="test_rls")

    def test_invalid_field_name_space(self):
        """Field name with space is rejected."""
        with pytest.raises(ValueError, match="Invalid field name"):
            RLSConstraint(field="tenant id", name="test_rls")

    def test_invalid_field_name_empty(self):
        """Empty field name is rejected."""
        with pytest.raises(ValueError, match="Invalid field name"):
            RLSConstraint(field="", name="test_rls")

    def test_invalid_pk_type(self):
        """Non-allowlisted tenant_pk_type is rejected."""
        with pytest.raises(ValueError, match="Invalid tenant_pk_type"):
            RLSConstraint(field="tenant", name="test_rls", tenant_pk_type="varchar")

    def test_invalid_pk_type_injection(self):
        """SQL injection via tenant_pk_type is rejected."""
        with pytest.raises(ValueError, match="Invalid tenant_pk_type"):
            RLSConstraint(field="tenant", name="test_rls", tenant_pk_type="int; DROP TABLE")

    def test_invalid_guc_tenant_var(self):
        """Invalid guc_tenant_var is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC name"):
            RLSConstraint(field="tenant", name="test_rls", guc_tenant_var="; DROP TABLE")

    def test_invalid_guc_admin_var(self):
        """Invalid guc_admin_var is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC name"):
            RLSConstraint(field="tenant", name="test_rls", guc_admin_var="a b")

    def test_invalid_extra_bypass_flag(self):
        """Invalid extra_bypass_flags entry is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC name"):
            RLSConstraint(
                field="tenant",
                name="test_rls",
                extra_bypass_flags=["rls.ok", "bad; --"],
            )

    def test_valid_inputs_accepted(self):
        """Valid inputs with all custom params are accepted."""
        c = RLSConstraint(
            field="organization",
            name="test_rls",
            guc_tenant_var="myapp.current_org",
            guc_admin_var="myapp.is_superuser",
            tenant_pk_type="uuid",
            extra_bypass_flags=["myapp.is_login"],
        )
        assert c.field == "organization"
        assert c.tenant_pk_type == "uuid"
