"""RLSConstraint -- a Django BaseConstraint for RLS policy management.

Generates ``CREATE POLICY`` / ``DROP POLICY`` SQL during migrations,
with idempotency checks, ``FORCE ROW LEVEL SECURITY``, and configurable
bypass flags.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from django.db.backends.ddl_references import Statement
from django.db.models import BaseConstraint

from django_rls_tenants.rls.guc import _GUC_NAME_RE

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.models import Model

# Allowed SQL cast types for tenant PK to prevent injection via tenant_pk_type.
_ALLOWED_PK_TYPES = frozenset({"int", "bigint", "uuid"})

# Valid SQL identifier for field names (no dots, just a column name).
_FIELD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_field_name(field: str) -> None:
    """Validate a field name for safe SQL interpolation.

    Raises:
        ValueError: If ``field`` contains invalid characters.
    """
    if not _FIELD_NAME_RE.match(field):
        msg = (
            f"Invalid field name: {field!r}. "
            f"Field names must match [a-zA-Z_][a-zA-Z0-9_]* "
            f"(e.g., 'tenant')."
        )
        raise ValueError(msg)


def _validate_pk_type(tenant_pk_type: str) -> None:
    """Validate tenant_pk_type against an allowlist.

    Raises:
        ValueError: If ``tenant_pk_type`` is not in the allowlist.
    """
    if tenant_pk_type not in _ALLOWED_PK_TYPES:
        msg = (
            f"Invalid tenant_pk_type: {tenant_pk_type!r}. "
            f"Allowed values: {', '.join(sorted(_ALLOWED_PK_TYPES))}."
        )
        raise ValueError(msg)


def _validate_guc_name_for_ddl(name: str, param: str) -> None:
    """Validate a GUC name used in DDL generation.

    Raises:
        ValueError: If ``name`` contains invalid characters.
    """
    if not _GUC_NAME_RE.match(name):
        msg = (
            f"Invalid GUC name for {param}: {name!r}. "
            f"GUC names must match [a-zA-Z_][a-zA-Z0-9_.]* "
            f"(e.g., 'rls.current_tenant')."
        )
        raise ValueError(msg)


class RLSConstraint(BaseConstraint):
    """Django constraint that generates PostgreSQL RLS policies during migrations.

    When Django applies a migration containing this constraint, it:

    1. Enables RLS on the table (``ALTER TABLE ... ENABLE ROW LEVEL SECURITY``).
    2. Forces RLS for the table owner (``ALTER TABLE ... FORCE ROW LEVEL SECURITY``).
    3. Creates an isolation policy with configurable ``USING`` and ``WITH CHECK``.

    Args:
        field: FK field name for tenant identification (e.g., ``"tenant"``).
            The policy checks ``{field}_id`` against the GUC variable.
        name: Constraint name. Supports ``%(app_label)s`` and ``%(class)s``.
        guc_tenant_var: GUC variable holding the current tenant ID.
            Default: ``"rls.current_tenant"``.
        guc_admin_var: GUC variable for admin bypass.
            Default: ``"rls.is_admin"``.
        tenant_pk_type: SQL cast type for tenant PK.
            Default: ``"int"``. Options: ``"int"``, ``"uuid"``, ``"bigint"``.
        extra_bypass_flags: Additional GUC variables that bypass the ``USING``
            clause (but NOT ``WITH CHECK``). Useful for auth edge cases.
    """

    def __init__(
        self,
        *,
        field: str,
        name: str,
        guc_tenant_var: str = "rls.current_tenant",
        guc_admin_var: str = "rls.is_admin",
        tenant_pk_type: str = "int",
        extra_bypass_flags: list[str] | None = None,
    ) -> None:
        super().__init__(name=name)
        _validate_field_name(field)
        _validate_pk_type(tenant_pk_type)
        _validate_guc_name_for_ddl(guc_tenant_var, "guc_tenant_var")
        _validate_guc_name_for_ddl(guc_admin_var, "guc_admin_var")
        for flag in extra_bypass_flags or []:
            _validate_guc_name_for_ddl(flag, "extra_bypass_flags")
        self.field = field
        self.guc_tenant_var = guc_tenant_var
        self.guc_admin_var = guc_admin_var
        self.tenant_pk_type = tenant_pk_type
        self.extra_bypass_flags = extra_bypass_flags or []

    def constraint_sql(  # type: ignore[override]
        self,
        model: type[Model],
        schema_editor: BaseDatabaseSchemaEditor,
    ) -> str:
        """No inline constraint SQL; defer RLS DDL to after ``CREATE TABLE``.

        Django calls ``constraint_sql`` during ``CREATE TABLE`` for inline
        constraints.  RLS policies require the table to exist first, so we
        defer the actual DDL and return an empty string (filtered out by
        Django's ``if statement`` guard).
        """
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return ""

    def create_sql(  # type: ignore[override]
        self,
        model: type[Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,  # noqa: ARG002  -- required by BaseConstraint
    ) -> Statement:
        """Generate SQL to enable RLS and create the isolation policy."""
        table = model._meta.db_table  # type: ignore[union-attr]  # noqa: SLF001  -- Django's standard public API
        policy_name = f"{table}_tenant_isolation_policy"

        tenant_match = (
            f"{self.field}_id = nullif("
            f"current_setting('{self.guc_tenant_var}', true), '')"
            f"::{self.tenant_pk_type}"
        )
        admin_check = f"current_setting('{self.guc_admin_var}', true) = 'true'"

        # Build bypass conditions for CASE WHEN: admin is always first,
        # extra bypass flags are appended (USING only, NOT WITH CHECK).
        bypass_conditions_using = [admin_check]
        bypass_conditions_using.extend(
            f"current_setting('{flag}', true) = 'true'" for flag in self.extra_bypass_flags
        )

        bypass_clause_using = "\n                              OR ".join(bypass_conditions_using)
        bypass_clause_check = admin_check  # only admin in WITH CHECK

        # Safety: all interpolated values come from model._meta (developer-defined)
        # and constructor args, not from user input. SQL injection is not possible.
        return Statement(
            template="%(sql)s",
            sql=f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE policyname = '{policy_name}'
                        AND tablename = '{table}'
                        AND schemaname = current_schema()
                    ) THEN
                        EXECUTE $BODY$
                            ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;
                            ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;
                            CREATE POLICY "{policy_name}"
                            ON "{table}"
                            USING (
                                CASE WHEN {bypass_clause_using}
                                     THEN true
                                     ELSE {tenant_match}
                                END
                            )
                            WITH CHECK (
                                CASE WHEN {bypass_clause_check}
                                     THEN true
                                     ELSE {tenant_match}
                                END
                            );
                        $BODY$;
                    END IF;
                END
                $$;
            """,
        )

    def remove_sql(  # type: ignore[override]
        self,
        model: type[Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,  # noqa: ARG002  -- required by BaseConstraint
    ) -> Statement:
        """Generate SQL to drop the policy and disable RLS."""
        table = model._meta.db_table  # type: ignore[union-attr]  # noqa: SLF001  -- Django's standard public API
        policy_name = f"{table}_tenant_isolation_policy"
        # Safety: values come from model._meta, not user input.
        return Statement(
            template="%(sql)s",
            sql=f"""
            DROP POLICY IF EXISTS "{policy_name}" ON "{table}";
            ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;
            ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;
            """,
        )

    def validate(
        self,
        model: type[Model],
        instance: Any,
        exclude: Any = None,
        using: str | None = None,
    ) -> None:
        """No-op: RLS is enforced at the database level, not in Django validation."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RLSConstraint):
            return (
                self.name == other.name
                and self.field == other.field
                and self.guc_tenant_var == other.guc_tenant_var
                and self.guc_admin_var == other.guc_admin_var
                and self.tenant_pk_type == other.tenant_pk_type
                and self.extra_bypass_flags == other.extra_bypass_flags
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(
            (
                self.name,
                self.field,
                self.guc_tenant_var,
                self.guc_admin_var,
                self.tenant_pk_type,
                tuple(self.extra_bypass_flags),
            )
        )

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, Any]]:
        """Return a 3-tuple for Django's migration serializer."""
        path, _, kwargs = super().deconstruct()
        kwargs["field"] = self.field
        if self.guc_tenant_var != "rls.current_tenant":
            kwargs["guc_tenant_var"] = self.guc_tenant_var
        if self.guc_admin_var != "rls.is_admin":
            kwargs["guc_admin_var"] = self.guc_admin_var
        if self.tenant_pk_type != "int":
            kwargs["tenant_pk_type"] = self.tenant_pk_type
        if self.extra_bypass_flags:
            kwargs["extra_bypass_flags"] = self.extra_bypass_flags
        return (path, (), kwargs)
