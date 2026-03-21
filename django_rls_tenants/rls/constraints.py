"""RLS constraints -- Django BaseConstraint subclasses for RLS policy management.

Provides ``RLSConstraint`` for standard tenant-FK-based policies, and
``RLSM2MConstraint`` for subquery-based policies on M2M join tables.

Both generate ``CREATE POLICY`` / ``DROP POLICY`` SQL during migrations,
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


def _validate_field_name(field: str, param: str = "field") -> None:
    """Validate a field name for safe SQL interpolation.

    Raises:
        ValueError: If ``field`` contains invalid characters.
    """
    if not _FIELD_NAME_RE.match(field):
        msg = (
            f"Invalid field name for {param}: {field!r}. "
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

    def __repr__(self) -> str:
        parts = [f"name={self.name!r}", f"field={self.field!r}"]
        if self.guc_tenant_var != "rls.current_tenant":
            parts.append(f"guc_tenant_var={self.guc_tenant_var!r}")
        if self.guc_admin_var != "rls.is_admin":
            parts.append(f"guc_admin_var={self.guc_admin_var!r}")
        if self.tenant_pk_type != "int":
            parts.append(f"tenant_pk_type={self.tenant_pk_type!r}")
        if self.extra_bypass_flags:
            parts.append(f"extra_bypass_flags={self.extra_bypass_flags!r}")
        return f"RLSConstraint({', '.join(parts)})"

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


# Valid dotted model path: "app_label.ModelName"
_MODEL_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_model_path(path: str, param: str) -> None:
    """Validate a dotted model path (e.g., ``"myapp.Order"``).

    Raises:
        ValueError: If ``path`` does not match ``app_label.ModelName``.
    """
    if not _MODEL_PATH_RE.match(path):
        msg = (
            f"Invalid model path for {param}: {path!r}. "
            f"Must be 'app_label.ModelName' (e.g., 'myapp.Project')."
        )
        raise ValueError(msg)


def _build_m2m_conditions(
    *,
    from_fk: str,
    from_table: str,
    from_tenant_fk: str | None,
    to_fk: str,
    to_table: str,
    to_tenant_fk: str | None,
    guc_tenant_var: str,
    tenant_pk_type: str,
) -> str:
    """Build tenant-checking EXISTS conditions for M2M RLS policies.

    Returns the ``ELSE`` expression used in both USING and WITH CHECK.
    Each protected side gets an ``EXISTS (SELECT 1 ...)`` subquery,
    which gives the PostgreSQL planner more optimisation flexibility
    than the equivalent ``IN (SELECT id ...)`` form.
    """
    guc_expr = f"nullif(current_setting('{guc_tenant_var}', true), '')::{tenant_pk_type}"
    conditions: list[str] = []

    if from_tenant_fk is not None:
        conditions.append(
            f"EXISTS ("
            f'SELECT 1 FROM "{from_table}" '
            f"WHERE id = {from_fk} AND {from_tenant_fk}_id = {guc_expr})"
        )

    if to_tenant_fk is not None:
        conditions.append(
            f"EXISTS ("
            f'SELECT 1 FROM "{to_table}" '
            f"WHERE id = {to_fk} AND {to_tenant_fk}_id = {guc_expr})"
        )

    return "\n                                AND ".join(conditions)


def _build_m2m_create_sql(*, table: str, admin_check: str, subquery_clause: str) -> str:
    """Build idempotent CREATE POLICY SQL for M2M RLS policies.

    Shared by ``RLSM2MConstraint``, ``AddM2MRLSPolicy``, and
    the ``setup_m2m_rls`` management command.
    """
    policy_name = f"{table}_m2m_rls_policy"
    return f"""
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
                                CASE WHEN {admin_check}
                                     THEN true
                                     ELSE {subquery_clause}
                                END
                            )
                            WITH CHECK (
                                CASE WHEN {admin_check}
                                     THEN true
                                     ELSE {subquery_clause}
                                END
                            );
                        $BODY$;
                    END IF;
                END
                $$;
            """


def _build_m2m_drop_sql(*, table: str) -> str:
    """Build DROP POLICY SQL for M2M RLS policies."""
    policy_name = f"{table}_m2m_rls_policy"
    return f"""
            DROP POLICY IF EXISTS "{policy_name}" ON "{table}";
            ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;
            ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;
            """


class RLSM2MConstraint(BaseConstraint):
    """Constraint generating subquery-based RLS policies for M2M join tables.

    Unlike ``RLSConstraint`` which checks a direct ``{field}_id`` column,
    this generates policies that verify both FK references point to rows
    belonging to the current tenant via ``IN (SELECT ...)`` subqueries.

    For join tables where only one side is RLS-protected, only that side's
    FK is checked. For both sides protected, both FKs are checked.

    Args:
        name: Constraint name.
        from_model: Dotted path to the "from" model (e.g., ``"myapp.Project"``).
        to_model: Dotted path to the "to" model (e.g., ``"myapp.User"``).
        from_fk: FK column name for the "from" side (e.g., ``"project_id"``).
        to_fk: FK column name for the "to" side (e.g., ``"user_id"``).
        from_tenant_fk: Tenant FK field name on the "from" model,
            or ``None`` if the "from" side is not RLS-protected.
            Default: ``"tenant"``.
        to_tenant_fk: Tenant FK field name on the "to" model,
            or ``None`` if the "to" side is not RLS-protected.
            Default: ``"tenant"``.
        guc_tenant_var: GUC variable for current tenant.
            Default: ``"rls.current_tenant"``.
        guc_admin_var: GUC variable for admin bypass.
            Default: ``"rls.is_admin"``.
        tenant_pk_type: SQL cast type for tenant PK.
            Default: ``"int"``.
    """

    def __init__(
        self,
        *,
        name: str,
        from_model: str,
        to_model: str,
        from_fk: str,
        to_fk: str,
        from_tenant_fk: str | None = "tenant",
        to_tenant_fk: str | None = "tenant",
        guc_tenant_var: str = "rls.current_tenant",
        guc_admin_var: str = "rls.is_admin",
        tenant_pk_type: str = "int",
    ) -> None:
        super().__init__(name=name)
        _validate_model_path(from_model, "from_model")
        _validate_model_path(to_model, "to_model")
        _validate_field_name(from_fk, "from_fk")
        _validate_field_name(to_fk, "to_fk")
        if from_tenant_fk is not None:
            _validate_field_name(from_tenant_fk, "from_tenant_fk")
        if to_tenant_fk is not None:
            _validate_field_name(to_tenant_fk, "to_tenant_fk")
        if from_tenant_fk is None and to_tenant_fk is None:
            msg = (
                "At least one side of the M2M relationship must be "
                "RLS-protected (from_tenant_fk or to_tenant_fk must be set)."
            )
            raise ValueError(msg)
        _validate_pk_type(tenant_pk_type)
        _validate_guc_name_for_ddl(guc_tenant_var, "guc_tenant_var")
        _validate_guc_name_for_ddl(guc_admin_var, "guc_admin_var")
        self.from_model = from_model
        self.to_model = to_model
        self.from_fk = from_fk
        self.to_fk = to_fk
        self.from_tenant_fk = from_tenant_fk
        self.to_tenant_fk = to_tenant_fk
        self.guc_tenant_var = guc_tenant_var
        self.guc_admin_var = guc_admin_var
        self.tenant_pk_type = tenant_pk_type

    def _resolve_table(self, model_path: str) -> str:
        """Resolve a dotted model path to its ``db_table``.

        Uses ``django.apps.apps.get_model()`` which is safe to call
        at migration time (models are loaded before operations run).
        """
        from django.apps import apps  # noqa: PLC0415

        model = apps.get_model(model_path)
        table: str = model._meta.db_table  # noqa: SLF001  -- Django standard API
        return table

    def _build_subquery_clause(self) -> str:
        """Build the tenant-checking subquery clauses for the policy."""
        return _build_m2m_conditions(
            from_fk=self.from_fk,
            from_table=self._resolve_table(self.from_model),
            from_tenant_fk=self.from_tenant_fk,
            to_fk=self.to_fk,
            to_table=self._resolve_table(self.to_model),
            to_tenant_fk=self.to_tenant_fk,
            guc_tenant_var=self.guc_tenant_var,
            tenant_pk_type=self.tenant_pk_type,
        )

    def constraint_sql(  # type: ignore[override]
        self,
        model: type[Model],
        schema_editor: BaseDatabaseSchemaEditor,
    ) -> str:
        """No inline constraint SQL; defer RLS DDL to after ``CREATE TABLE``."""
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return ""

    def create_sql(  # type: ignore[override]
        self,
        model: type[Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,  # noqa: ARG002  -- required by BaseConstraint
    ) -> Statement:
        """Generate SQL to enable RLS and create the M2M isolation policy."""
        table = model._meta.db_table  # type: ignore[union-attr]  # noqa: SLF001
        admin_check = f"current_setting('{self.guc_admin_var}', true) = 'true'"
        subquery_clause = self._build_subquery_clause()
        return Statement(
            template="%(sql)s",
            sql=_build_m2m_create_sql(
                table=table, admin_check=admin_check, subquery_clause=subquery_clause
            ),
        )

    def remove_sql(  # type: ignore[override]
        self,
        model: type[Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,  # noqa: ARG002  -- required by BaseConstraint
    ) -> Statement:
        """Generate SQL to drop the M2M policy and disable RLS."""
        table = model._meta.db_table  # type: ignore[union-attr]  # noqa: SLF001
        return Statement(
            template="%(sql)s",
            sql=_build_m2m_drop_sql(table=table),
        )

    def validate(
        self,
        model: type[Model],
        instance: Any,
        exclude: Any = None,
        using: str | None = None,
    ) -> None:
        """No-op: RLS is enforced at the database level, not in Django validation."""

    def __repr__(self) -> str:
        parts = [
            f"name={self.name!r}",
            f"from_model={self.from_model!r}",
            f"to_model={self.to_model!r}",
            f"from_fk={self.from_fk!r}",
            f"to_fk={self.to_fk!r}",
        ]
        if self.from_tenant_fk != "tenant":
            parts.append(f"from_tenant_fk={self.from_tenant_fk!r}")
        if self.to_tenant_fk != "tenant":
            parts.append(f"to_tenant_fk={self.to_tenant_fk!r}")
        if self.guc_tenant_var != "rls.current_tenant":
            parts.append(f"guc_tenant_var={self.guc_tenant_var!r}")
        if self.guc_admin_var != "rls.is_admin":
            parts.append(f"guc_admin_var={self.guc_admin_var!r}")
        if self.tenant_pk_type != "int":
            parts.append(f"tenant_pk_type={self.tenant_pk_type!r}")
        return f"RLSM2MConstraint({', '.join(parts)})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RLSM2MConstraint):
            return (
                self.name == other.name
                and self.from_model == other.from_model
                and self.to_model == other.to_model
                and self.from_fk == other.from_fk
                and self.to_fk == other.to_fk
                and self.from_tenant_fk == other.from_tenant_fk
                and self.to_tenant_fk == other.to_tenant_fk
                and self.guc_tenant_var == other.guc_tenant_var
                and self.guc_admin_var == other.guc_admin_var
                and self.tenant_pk_type == other.tenant_pk_type
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(
            (
                self.name,
                self.from_model,
                self.to_model,
                self.from_fk,
                self.to_fk,
                self.from_tenant_fk,
                self.to_tenant_fk,
                self.guc_tenant_var,
                self.guc_admin_var,
                self.tenant_pk_type,
            )
        )

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, Any]]:
        """Return a 3-tuple for Django's migration serializer."""
        path, _, kwargs = super().deconstruct()
        kwargs["from_model"] = self.from_model
        kwargs["to_model"] = self.to_model
        kwargs["from_fk"] = self.from_fk
        kwargs["to_fk"] = self.to_fk
        if self.from_tenant_fk != "tenant":
            kwargs["from_tenant_fk"] = self.from_tenant_fk
        if self.to_tenant_fk != "tenant":
            kwargs["to_tenant_fk"] = self.to_tenant_fk
        if self.guc_tenant_var != "rls.current_tenant":
            kwargs["guc_tenant_var"] = self.guc_tenant_var
        if self.guc_admin_var != "rls.is_admin":
            kwargs["guc_admin_var"] = self.guc_admin_var
        if self.tenant_pk_type != "int":
            kwargs["tenant_pk_type"] = self.tenant_pk_type
        return (path, (), kwargs)
