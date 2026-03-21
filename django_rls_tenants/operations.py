"""Custom migration operations for django-rls-tenants.

Provides ``AddM2MRLSPolicy`` for adding subquery-based RLS policies
to M2M join tables via Django migrations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import migrations

from django_rls_tenants.rls.constraints import (
    _build_m2m_conditions,
    _build_m2m_create_sql,
    _build_m2m_drop_sql,
    _validate_field_name,
    _validate_guc_name_for_ddl,
    _validate_model_path,
    _validate_pk_type,
)

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import ProjectState


class AddM2MRLSPolicy(migrations.operations.base.Operation):
    """Migration operation to add an RLS policy to an M2M through table.

    Generates and executes subquery-based ``CREATE POLICY`` SQL that
    checks both FK sides of the M2M join table belong to the current
    tenant. Supports tables where only one side is RLS-protected.

    This operation is reversible: ``database_backwards`` drops the policy
    and disables RLS on the table.

    Args:
        m2m_table: The database table name of the M2M through table.
        from_model: Dotted path to the "from" model (e.g., ``"myapp.Project"``).
        to_model: Dotted path to the "to" model (e.g., ``"myapp.User"``).
        from_fk: FK column on the through table for the "from" side.
        to_fk: FK column on the through table for the "to" side.
        from_tenant_fk: Tenant FK on the "from" model, or ``None``.
            Default: ``"tenant"``.
        to_tenant_fk: Tenant FK on the "to" model, or ``None``.
            Default: ``"tenant"``.
        guc_tenant_var: GUC variable for current tenant.
            Default: ``"rls.current_tenant"``.
        guc_admin_var: GUC variable for admin bypass.
            Default: ``"rls.is_admin"``.
        tenant_pk_type: SQL cast type for tenant PK.
            Default: ``"int"``.
    """

    reduces_to_sql = True
    reversible = True

    def __init__(
        self,
        m2m_table: str,
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
        _validate_field_name(m2m_table, "m2m_table")
        _validate_model_path(from_model, "from_model")
        _validate_model_path(to_model, "to_model")
        _validate_field_name(from_fk, "from_fk")
        _validate_field_name(to_fk, "to_fk")
        if from_tenant_fk is not None:
            _validate_field_name(from_tenant_fk, "from_tenant_fk")
        if to_tenant_fk is not None:
            _validate_field_name(to_tenant_fk, "to_tenant_fk")
        _validate_pk_type(tenant_pk_type)
        _validate_guc_name_for_ddl(guc_tenant_var, "guc_tenant_var")
        _validate_guc_name_for_ddl(guc_admin_var, "guc_admin_var")
        self.m2m_table = m2m_table
        self.from_model = from_model
        self.to_model = to_model
        self.from_fk = from_fk
        self.to_fk = to_fk
        self.from_tenant_fk = from_tenant_fk
        self.to_tenant_fk = to_tenant_fk
        self.guc_tenant_var = guc_tenant_var
        self.guc_admin_var = guc_admin_var
        self.tenant_pk_type = tenant_pk_type

    def _resolve_table(self, model_path: str, apps: Any) -> str:
        """Resolve a dotted model path to its ``db_table``."""
        app_label, model_name = model_path.split(".")
        model = apps.get_model(app_label, model_name)
        table: str = model._meta.db_table  # noqa: SLF001
        return table

    def state_forwards(
        self,
        app_label: str,
        state: ProjectState,
    ) -> None:
        """No model state changes -- this is a database-only operation."""

    def database_forwards(
        self,
        app_label: str,  # noqa: ARG002
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,  # noqa: ARG002
        to_state: ProjectState,
    ) -> None:
        """Create the M2M RLS policy."""
        table = self.m2m_table
        admin_check = f"current_setting('{self.guc_admin_var}', true) = 'true'"
        subquery_clause = _build_m2m_conditions(
            from_fk=self.from_fk,
            from_table=self._resolve_table(self.from_model, to_state.apps),
            from_tenant_fk=self.from_tenant_fk,
            to_fk=self.to_fk,
            to_table=self._resolve_table(self.to_model, to_state.apps),
            to_tenant_fk=self.to_tenant_fk,
            guc_tenant_var=self.guc_tenant_var,
            tenant_pk_type=self.tenant_pk_type,
        )
        sql = _build_m2m_create_sql(
            table=table, admin_check=admin_check, subquery_clause=subquery_clause
        )
        schema_editor.execute(sql)

    def database_backwards(
        self,
        app_label: str,  # noqa: ARG002
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,  # noqa: ARG002
        to_state: ProjectState,  # noqa: ARG002
    ) -> None:
        """Drop the M2M RLS policy and disable RLS."""
        schema_editor.execute(_build_m2m_drop_sql(table=self.m2m_table))

    def describe(self) -> str:
        """Return a human-readable description."""
        return f"Add M2M RLS policy to {self.m2m_table}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Return args for Django's migration serializer."""
        kwargs: dict[str, Any] = {
            "m2m_table": self.m2m_table,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "from_fk": self.from_fk,
            "to_fk": self.to_fk,
        }
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
        return (
            f"{self.__class__.__module__}.{self.__class__.__qualname__}",
            [],
            kwargs,
        )
