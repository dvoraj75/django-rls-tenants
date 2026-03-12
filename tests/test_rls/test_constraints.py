"""Tests for django_rls_tenants.rls.constraints."""

from __future__ import annotations

from unittest.mock import MagicMock

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
