"""Tests for django_rls_tenants.tenants.sql (issue #33).

These cover the raw-SQL helpers ``safe_tenant_sql`` and
``current_tenant_value_sql``: the exact fragments they emit, that they reuse the
InitPlan-wrapped policy SQL (#57), how they honour ``RLS_TENANTS`` config, and
that every interpolated identifier is validated.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from django.test import override_settings

from django_rls_tenants.rls.policy_sql import bool_flag_sql, tenant_match_sql, tenant_value_sql
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.sql import current_tenant_value_sql, safe_tenant_sql

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def _override_rls_tenants(**rls_tenants: object) -> Iterator[None]:
    """``override_settings(RLS_TENANTS=...)`` that also resets the cached config.

    ``rls_tenants_config`` caches the settings dict on first read and has no
    settings-changed hook, so the cache must be cleared before and after the
    override for the new values to take effect (same pattern as test_context.py).
    """
    rls_tenants.setdefault("TENANT_MODEL", "test_app.Tenant")
    with override_settings(RLS_TENANTS=rls_tenants):
        rls_tenants_config._config_cache = None
        rls_tenants_config._unknown_keys_checked = False
        try:
            yield
        finally:
            rls_tenants_config._config_cache = None
            rls_tenants_config._unknown_keys_checked = False


# Default test settings: GUC_PREFIX="rls", TENANT_PK_TYPE="int".
_DEFAULT_PREDICATE = (
    "tenant_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
)
_DEFAULT_ADMIN = "(SELECT current_setting('rls.is_admin', true)) = 'true'"


class TestSafeTenantSql:
    """Tests for safe_tenant_sql()."""

    def test_default(self):
        """Default column + include_admin wraps the match OR the admin flag."""
        assert safe_tenant_sql() == f"({_DEFAULT_PREDICATE} OR {_DEFAULT_ADMIN})"

    def test_include_admin_false_is_bare_predicate(self):
        """include_admin=False drops the admin branch and the wrapping parens."""
        assert safe_tenant_sql(include_admin=False) == _DEFAULT_PREDICATE

    def test_custom_column(self):
        """A custom column name is interpolated verbatim."""
        assert safe_tenant_sql("org_id", include_admin=False) == (
            "org_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
        )

    def test_table_qualified_column(self):
        """table= double-quotes the table and qualifies the (unquoted) column."""
        assert safe_tenant_sql(table="orders", include_admin=False) == (
            '"orders".tenant_id = '
            "nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
        )

    def test_table_qualified_with_admin(self):
        """table= composes with the admin branch."""
        assert safe_tenant_sql("tenant_id", table="orders") == (
            '("orders".tenant_id = '
            "nullif((SELECT current_setting('rls.current_tenant', true)), '')::int "
            "OR (SELECT current_setting('rls.is_admin', true)) = 'true')"
        )

    def test_custom_column_and_table_combined(self):
        """A custom column and a table qualifier compose: ``"orders".org_id``."""
        assert safe_tenant_sql("org_id", table="orders", include_admin=False) == (
            '"orders".org_id = '
            "nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
        )

    def test_include_admin_defaults_true(self):
        """The admin branch is present unless explicitly disabled."""
        assert " OR " in safe_tenant_sql()
        assert " OR " not in safe_tenant_sql(include_admin=False)

    def test_matches_policy_predicate_exactly(self):
        """The bare fragment is byte-for-byte the policy's tenant-match expression (#57).

        This is the policy's ``ELSE`` branch; the admin-inclusive form spells the
        bypass as ``OR`` rather than the policy's ``CASE WHEN`` (same truth table,
        different string), so only the bare predicate is compared here.
        """
        conf = rls_tenants_config
        expected = tenant_match_sql("tenant_id", conf.GUC_CURRENT_TENANT, conf.TENANT_PK_TYPE)
        assert safe_tenant_sql(include_admin=False) == expected

    def test_reads_are_initplan_wrapped(self):
        """GUC reads use the (SELECT current_setting(...)) InitPlan form, not inline."""
        sql = safe_tenant_sql()
        assert "(SELECT current_setting(" in sql
        # The pre-#57 inline read must not appear.
        assert "nullif(current_setting(" not in sql

    def test_no_bind_params(self):
        """The fragment carries no %s placeholders -- the tenant id is read in-DB."""
        assert "%s" not in safe_tenant_sql()
        assert "%s" not in safe_tenant_sql(include_admin=False)


class TestExtraBypassFlags:
    """safe_tenant_sql() mirrors RLSConstraint's extra_bypass_flags bypass set."""

    _LOGIN_FLAG = "(SELECT current_setting('rls.is_login_request', true)) = 'true'"
    _PREAUTH_FLAG = "(SELECT current_setting('rls.is_preauth_request', true)) = 'true'"

    def test_single_extra_flag_appended_after_admin(self):
        """An extra flag is OR-ed in after the admin flag (policy ordering)."""
        assert safe_tenant_sql(extra_bypass_flags=["rls.is_login_request"]) == (
            f"({_DEFAULT_PREDICATE} OR {_DEFAULT_ADMIN} OR {self._LOGIN_FLAG})"
        )

    def test_multiple_extra_flags_keep_order(self):
        """Multiple flags are appended in the given order, after admin."""
        assert safe_tenant_sql(
            extra_bypass_flags=["rls.is_login_request", "rls.is_preauth_request"]
        ) == (
            f"({_DEFAULT_PREDICATE} OR {_DEFAULT_ADMIN} "
            f"OR {self._LOGIN_FLAG} OR {self._PREAUTH_FLAG})"
        )

    def test_empty_list_matches_no_flags(self):
        """An empty list is equivalent to omitting the argument."""
        assert safe_tenant_sql(extra_bypass_flags=[]) == safe_tenant_sql()

    def test_flags_ignored_when_include_admin_false(self):
        """include_admin=False scopes strictly, ignoring extra bypass flags."""
        assert safe_tenant_sql(
            extra_bypass_flags=["rls.is_login_request"], include_admin=False
        ) == safe_tenant_sql(include_admin=False)

    def test_flags_use_initplan_bool_flag_helper(self):
        """Each flag is emitted via the same InitPlan-wrapped helper as the policy."""
        sql = safe_tenant_sql(extra_bypass_flags=["rls.is_login_request"])
        assert bool_flag_sql("rls.is_login_request") in sql

    def test_invalid_extra_flag_raises(self):
        """A malformed flag GUC name is rejected before producing SQL."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            safe_tenant_sql(extra_bypass_flags=["bad flag"])


class TestCurrentTenantValueSql:
    """Tests for current_tenant_value_sql()."""

    def test_default(self):
        """Emits the cast GUC read used on the right-hand side of the policy."""
        assert current_tenant_value_sql() == (
            "nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
        )

    def test_matches_policy_value_exactly(self):
        """Byte-for-byte the policy's tenant-value expression (#57)."""
        conf = rls_tenants_config
        expected = tenant_value_sql(conf.GUC_CURRENT_TENANT, conf.TENANT_PK_TYPE)
        assert current_tenant_value_sql() == expected

    def test_reads_are_initplan_wrapped(self):
        """The GUC read is wrapped in a scalar sub-SELECT, not inline."""
        sql = current_tenant_value_sql()
        assert "(SELECT current_setting(" in sql
        assert "nullif(current_setting(" not in sql


class TestConfigDerived:
    """safe_tenant_sql() / current_tenant_value_sql() honour RLS_TENANTS."""

    def test_custom_guc_prefix(self):
        """A custom GUC_PREFIX flows into the tenant, admin, and value reads."""
        with _override_rls_tenants(GUC_PREFIX="myco"):
            assert safe_tenant_sql() == (
                "(tenant_id = "
                "nullif((SELECT current_setting('myco.current_tenant', true)), '')::int "
                "OR (SELECT current_setting('myco.is_admin', true)) = 'true')"
            )
            assert current_tenant_value_sql() == (
                "nullif((SELECT current_setting('myco.current_tenant', true)), '')::int"
            )

    def test_uuid_pk_type(self):
        """TENANT_PK_TYPE drives the ::cast on both helpers."""
        with _override_rls_tenants(TENANT_PK_TYPE="uuid"):
            value = "nullif((SELECT current_setting('rls.current_tenant', true)), '')::uuid"
            assert safe_tenant_sql(include_admin=False) == f"tenant_id = {value}"
            assert current_tenant_value_sql() == value

    def test_bigint_pk_type(self):
        """bigint is in the PK-type allowlist and drives the ::cast on both helpers."""
        with _override_rls_tenants(TENANT_PK_TYPE="bigint"):
            value = "nullif((SELECT current_setting('rls.current_tenant', true)), '')::bigint"
            assert safe_tenant_sql(include_admin=False) == f"tenant_id = {value}"
            assert current_tenant_value_sql() == value


class TestValidation:
    """Every interpolated identifier is validated against the RLS allowlists."""

    def test_invalid_column_raises(self):
        """A non-identifier column is rejected, not interpolated."""
        with pytest.raises(ValueError, match="Invalid field name for column"):
            safe_tenant_sql("tenant_id; DROP TABLE orders")

    def test_invalid_column_with_dash_raises(self):
        with pytest.raises(ValueError, match="Invalid field name for column"):
            safe_tenant_sql("bad-column")

    def test_invalid_table_raises(self):
        """A non-identifier table is rejected even though it is double-quoted."""
        with pytest.raises(ValueError, match="Invalid field name for table"):
            safe_tenant_sql("tenant_id", table='orders" OR "1"="1')

    def test_empty_column_raises(self):
        """An empty column name is rejected (regex requires >= 1 char)."""
        with pytest.raises(ValueError, match="Invalid field name for column"):
            safe_tenant_sql("")

    def test_empty_table_raises(self):
        """An empty table name is rejected."""
        with pytest.raises(ValueError, match="Invalid field name for table"):
            safe_tenant_sql("tenant_id", table="")

    def test_column_with_trailing_newline_raises(self):
        """A trailing newline is rejected (\\Z anchor, not $)."""
        with pytest.raises(ValueError, match="Invalid field name for column"):
            safe_tenant_sql("tenant_id\n")

    def test_invalid_guc_prefix_raises(self):
        """A malformed GUC_PREFIX is rejected before producing SQL."""
        with (
            _override_rls_tenants(GUC_PREFIX="bad prefix"),
            pytest.raises(ValueError, match="Invalid GUC variable name"),
        ):
            safe_tenant_sql()

    def test_invalid_pk_type_raises(self):
        """A TENANT_PK_TYPE outside the allowlist is rejected."""
        with (
            _override_rls_tenants(TENANT_PK_TYPE="text"),
            pytest.raises(ValueError, match="Invalid tenant_pk_type"),
        ):
            safe_tenant_sql()

    def test_current_tenant_value_sql_invalid_guc_prefix_raises(self):
        """current_tenant_value_sql() also rejects a malformed GUC_PREFIX."""
        with (
            _override_rls_tenants(GUC_PREFIX="bad prefix"),
            pytest.raises(ValueError, match="Invalid GUC variable name"),
        ):
            current_tenant_value_sql()

    def test_current_tenant_value_sql_invalid_pk_type_raises(self):
        """current_tenant_value_sql() also rejects a bad TENANT_PK_TYPE."""
        with (
            _override_rls_tenants(TENANT_PK_TYPE="text"),
            pytest.raises(ValueError, match="Invalid tenant_pk_type"),
        ):
            current_tenant_value_sql()
