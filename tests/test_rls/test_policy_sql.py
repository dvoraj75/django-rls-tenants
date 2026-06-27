"""Tests for django_rls_tenants.rls.policy_sql.

These cover the InitPlan-wrapping helpers (issue #57): every GUC read is wrapped
in an uncorrelated scalar sub-SELECT, ``(SELECT current_setting('<guc>', true))``,
so PostgreSQL evaluates it once per statement instead of per row.
"""

from __future__ import annotations

from django_rls_tenants.rls.policy_sql import (
    bool_flag_sql,
    scalar_setting,
    tenant_match_sql,
    tenant_value_sql,
)


class TestScalarSetting:
    """Tests for scalar_setting()."""

    def test_wraps_current_setting_in_subselect(self):
        assert scalar_setting("rls.current_tenant") == (
            "(SELECT current_setting('rls.current_tenant', true))"
        )

    def test_custom_guc_name(self):
        assert scalar_setting("myco.is_admin") == (
            "(SELECT current_setting('myco.is_admin', true))"
        )


class TestTenantValueSql:
    """Tests for tenant_value_sql()."""

    def test_int_pk(self):
        assert tenant_value_sql("rls.current_tenant", "int") == (
            "nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
        )

    def test_uuid_pk(self):
        assert tenant_value_sql("rls.current_tenant", "uuid") == (
            "nullif((SELECT current_setting('rls.current_tenant', true)), '')::uuid"
        )

    def test_read_is_wrapped_not_inline(self):
        sql = tenant_value_sql("rls.current_tenant", "int")
        # The InitPlan win: current_setting lives inside a scalar sub-SELECT.
        assert "(SELECT current_setting(" in sql
        # The pre-#57 inline form must be gone.
        assert "nullif(current_setting(" not in sql


class TestTenantMatchSql:
    """Tests for tenant_match_sql()."""

    def test_default(self):
        assert tenant_match_sql("tenant_id", "rls.current_tenant", "int") == (
            "tenant_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
        )

    def test_table_qualified_column(self):
        sql = tenant_match_sql('"orders".tenant_id', "rls.current_tenant", "int")
        assert sql.startswith('"orders".tenant_id = ')

    def test_is_column_equals_tenant_value(self):
        # tenant_match_sql is exactly "<column> = <tenant_value_sql(...)>".
        col, guc, pk = "tenant_id", "rls.current_tenant", "int"
        assert tenant_match_sql(col, guc, pk) == f"{col} = {tenant_value_sql(guc, pk)}"


class TestBoolFlagSql:
    """Tests for bool_flag_sql()."""

    def test_default(self):
        assert bool_flag_sql("rls.is_admin") == (
            "(SELECT current_setting('rls.is_admin', true)) = 'true'"
        )

    def test_custom_flag(self):
        assert bool_flag_sql("rls.is_login_request") == (
            "(SELECT current_setting('rls.is_login_request', true)) = 'true'"
        )

    def test_read_is_wrapped_not_inline(self):
        # The pre-#57 inline form ("current_setting('x', true) = 'true'") is gone.
        assert bool_flag_sql("rls.is_admin").startswith("(SELECT current_setting(")
