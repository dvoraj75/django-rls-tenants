"""Tests for django_rls_tenants.rls.constraints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from django_rls_tenants.rls.constraints import RLSConstraint, RLSM2MConstraint


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


# ===========================================================================
# RLSM2MConstraint tests
# ===========================================================================


def _make_m2m_constraint(**overrides):
    """Create an RLSM2MConstraint with sensible defaults."""
    defaults = {
        "name": "test_m2m_rls",
        "from_model": "myapp.Project",
        "to_model": "myapp.User",
        "from_fk": "project_id",
        "to_fk": "user_id",
        "from_tenant_fk": "tenant",
        "to_tenant_fk": "tenant",
    }
    defaults.update(overrides)
    return RLSM2MConstraint(**defaults)


def _mock_get_model(model_path):
    """Return a mock model with matching db_table for apps.get_model()."""
    tables = {
        "myapp.Project": "myapp_project",
        "myapp.User": "myapp_user",
        "myapp.Tag": "myapp_tag",
        "myapp.SelfRef": "myapp_selfref",
    }
    model = MagicMock()
    model._meta.db_table = tables.get(model_path, model_path.lower().replace(".", "_"))
    return model


def _get_m2m_create_sql(constraint, db_table="myapp_project_users"):
    """Get SQL from create_sql() with mocked model resolution."""
    model = MagicMock()
    model._meta.db_table = db_table
    target = "django_rls_tenants.rls.constraints.RLSM2MConstraint._resolve_table"
    with patch(target) as mock_resolve:
        mock_resolve.side_effect = lambda p: _mock_get_model(p)._meta.db_table
        statement = constraint.create_sql(model, schema_editor=MagicMock())
    return str(statement)


def _get_m2m_remove_sql(constraint, db_table="myapp_project_users"):
    """Get SQL from remove_sql()."""
    model = MagicMock()
    model._meta.db_table = db_table
    statement = constraint.remove_sql(model, schema_editor=MagicMock())
    return str(statement)


class TestM2MCreateSQL:
    """Tests for RLSM2MConstraint.create_sql()."""

    def test_generates_enable_rls(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_create_sql(c)
        assert "ENABLE ROW LEVEL SECURITY" in sql

    def test_generates_force_rls(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_create_sql(c)
        assert "FORCE ROW LEVEL SECURITY" in sql

    def test_generates_policy_with_using_and_check(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_create_sql(c)
        assert "CREATE POLICY" in sql
        assert "USING" in sql
        assert "WITH CHECK" in sql

    def test_policy_name_contains_m2m_rls(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_create_sql(c, db_table="project_users")
        assert "project_users_m2m_rls_policy" in sql

    def test_both_sides_rls_protected(self):
        """Both FK sides generate EXISTS subqueries."""
        c = _make_m2m_constraint(from_tenant_fk="tenant", to_tenant_fk="tenant")
        sql = _get_m2m_create_sql(c)
        assert "EXISTS (SELECT 1 FROM" in sql
        assert "WHERE id = project_id AND tenant_id" in sql
        assert "WHERE id = user_id AND tenant_id" in sql

    def test_one_side_rls_protected(self):
        """Only the protected side generates a subquery."""
        c = _make_m2m_constraint(to_tenant_fk=None)
        sql = _get_m2m_create_sql(c)
        assert "WHERE id = project_id AND tenant_id" in sql
        assert "WHERE id = user_id" not in sql

    def test_self_referential_m2m(self):
        """Self-referential: both FKs check the same table."""
        c = _make_m2m_constraint(
            from_model="myapp.SelfRef",
            to_model="myapp.SelfRef",
            from_fk="from_selfref_id",
            to_fk="to_selfref_id",
        )
        sql = _get_m2m_create_sql(c, db_table="myapp_selfref_friends")
        assert "WHERE id = from_selfref_id AND tenant_id" in sql
        assert "WHERE id = to_selfref_id AND tenant_id" in sql
        # Both should reference the same table
        assert sql.count('"myapp_selfref"') >= 2

    def test_admin_bypass_in_policy(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_create_sql(c)
        assert "rls.is_admin" in sql
        assert "CASE WHEN" in sql

    def test_idempotent_if_not_exists(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_create_sql(c)
        assert "IF NOT EXISTS" in sql

    def test_tenant_pk_type_uuid(self):
        c = _make_m2m_constraint(tenant_pk_type="uuid")
        sql = _get_m2m_create_sql(c)
        assert "::uuid" in sql

    def test_custom_guc_vars(self):
        c = _make_m2m_constraint(
            guc_tenant_var="app.org_id",
            guc_admin_var="app.superuser",
        )
        sql = _get_m2m_create_sql(c)
        assert "app.org_id" in sql
        assert "app.superuser" in sql

    def test_custom_tenant_fk_field_names(self):
        c = _make_m2m_constraint(from_tenant_fk="organization", to_tenant_fk="org")
        sql = _get_m2m_create_sql(c)
        assert "organization_id" in sql
        assert "org_id" in sql


class TestM2MRemoveSQL:
    """Tests for RLSM2MConstraint.remove_sql()."""

    def test_drops_policy(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_remove_sql(c)
        assert "DROP POLICY IF EXISTS" in sql

    def test_no_force_rls(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_remove_sql(c)
        assert "NO FORCE ROW LEVEL SECURITY" in sql

    def test_disables_rls(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_remove_sql(c)
        assert "DISABLE ROW LEVEL SECURITY" in sql

    def test_policy_name(self):
        c = _make_m2m_constraint()
        sql = _get_m2m_remove_sql(c, db_table="myapp_project_users")
        assert "myapp_project_users_m2m_rls_policy" in sql


class TestM2MDeconstruct:
    """Tests for RLSM2MConstraint.deconstruct()."""

    def test_roundtrip(self):
        c = _make_m2m_constraint(
            guc_tenant_var="app.tenant",
            guc_admin_var="app.admin",
            tenant_pk_type="uuid",
            from_tenant_fk="org",
            to_tenant_fk="org",
        )
        _path, _args, kwargs = c.deconstruct()
        reconstructed = RLSM2MConstraint(**kwargs)
        assert c == reconstructed

    def test_defaults_omitted(self):
        c = _make_m2m_constraint()
        _, _, kwargs = c.deconstruct()
        assert "from_model" in kwargs
        assert "to_model" in kwargs
        assert "from_fk" in kwargs
        assert "to_fk" in kwargs
        # Defaults should be omitted
        assert "from_tenant_fk" not in kwargs
        assert "to_tenant_fk" not in kwargs
        assert "guc_tenant_var" not in kwargs
        assert "guc_admin_var" not in kwargs
        assert "tenant_pk_type" not in kwargs

    def test_non_defaults_included(self):
        c = _make_m2m_constraint(
            from_tenant_fk="org",
            to_tenant_fk=None,
            guc_tenant_var="custom.tenant",
            tenant_pk_type="bigint",
        )
        _, _, kwargs = c.deconstruct()
        assert kwargs["from_tenant_fk"] == "org"
        assert kwargs["to_tenant_fk"] is None
        assert kwargs["guc_tenant_var"] == "custom.tenant"
        assert kwargs["tenant_pk_type"] == "bigint"


class TestM2MEquality:
    """Tests for RLSM2MConstraint.__eq__ and __hash__."""

    def test_eq_same_params(self):
        a = _make_m2m_constraint()
        b = _make_m2m_constraint()
        assert a == b

    def test_eq_different_params(self):
        a = _make_m2m_constraint(from_fk="project_id")
        b = _make_m2m_constraint(from_fk="order_id")
        assert a != b

    def test_hash_same_params(self):
        a = _make_m2m_constraint()
        b = _make_m2m_constraint()
        assert hash(a) == hash(b)

    def test_hash_different_params(self):
        a = _make_m2m_constraint(name="a")
        b = _make_m2m_constraint(name="b")
        assert hash(a) != hash(b)

    def test_not_equal_to_rls_constraint(self):
        m2m = _make_m2m_constraint()
        rls = RLSConstraint(field="tenant", name="test_m2m_rls")
        assert m2m != rls


class TestM2MInputValidation:
    """Tests for RLSM2MConstraint input validation."""

    def test_invalid_from_model_path(self):
        with pytest.raises(ValueError, match="Invalid model path"):
            _make_m2m_constraint(from_model="bad")

    def test_invalid_to_model_path(self):
        with pytest.raises(ValueError, match="Invalid model path"):
            _make_m2m_constraint(to_model="no_dot")

    def test_invalid_from_fk(self):
        with pytest.raises(ValueError, match="Invalid field name"):
            _make_m2m_constraint(from_fk="bad; sql")

    def test_invalid_to_fk(self):
        with pytest.raises(ValueError, match="Invalid field name"):
            _make_m2m_constraint(to_fk="")

    def test_invalid_from_tenant_fk(self):
        with pytest.raises(ValueError, match="Invalid field name"):
            _make_m2m_constraint(from_tenant_fk="bad.field")

    def test_invalid_to_tenant_fk(self):
        with pytest.raises(ValueError, match="Invalid field name"):
            _make_m2m_constraint(to_tenant_fk="has space")

    def test_both_tenant_fk_none_rejected(self):
        with pytest.raises(ValueError, match="At least one side"):
            _make_m2m_constraint(from_tenant_fk=None, to_tenant_fk=None)

    def test_invalid_pk_type(self):
        with pytest.raises(ValueError, match="Invalid tenant_pk_type"):
            _make_m2m_constraint(tenant_pk_type="text")

    def test_invalid_guc_tenant_var(self):
        with pytest.raises(ValueError, match="Invalid GUC name"):
            _make_m2m_constraint(guc_tenant_var="bad; sql")

    def test_invalid_guc_admin_var(self):
        with pytest.raises(ValueError, match="Invalid GUC name"):
            _make_m2m_constraint(guc_admin_var="a b")

    def test_valid_with_one_side_none(self):
        """One side can be None (non-RLS model)."""
        c = _make_m2m_constraint(to_tenant_fk=None)
        assert c.to_tenant_fk is None
        assert c.from_tenant_fk == "tenant"
