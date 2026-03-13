"""RLSConstraint -- a Django BaseConstraint for RLS policy management.

Generates ``CREATE POLICY`` / ``DROP POLICY`` SQL during migrations,
with idempotency checks, ``FORCE ROW LEVEL SECURITY``, and configurable
bypass flags.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.backends.ddl_references import Statement
from django.db.models import BaseConstraint

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.models import Model


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
            f"{self.field}_id = coalesce("
            f"nullif(current_setting('{self.guc_tenant_var}', true), '')"
            f"::{self.tenant_pk_type}, NULL)"
        )
        admin_bypass = f"coalesce(current_setting('{self.guc_admin_var}', true) = 'true', false)"

        # Extra bypass flags apply to USING (SELECT) only, NOT WITH CHECK (INSERT/UPDATE).
        extra_using_clauses = ""
        for flag in self.extra_bypass_flags:
            extra_using_clauses += (
                "\n                                OR coalesce("
                f"current_setting('{flag}', true) = 'true', false)"
            )

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
                                {tenant_match}
                                OR {admin_bypass}{extra_using_clauses}
                            )
                            WITH CHECK (
                                {tenant_match}
                                OR {admin_bypass}
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
