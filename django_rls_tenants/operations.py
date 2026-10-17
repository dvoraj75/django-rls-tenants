"""Custom migration operations for django-rls-tenants.

Provides ``AddRLSPolicy`` for adding the tenant policy ``RLSConstraint`` creates
to a table whose model the project cannot change (a third-party app's), and
``AddM2MRLSPolicy`` for adding subquery-based RLS policies to M2M join tables
via Django migrations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import migrations, models, router
from django.db.migrations.operations.base import OperationCategory

from django_rls_tenants.rls.constraints import (
    RLSConstraint,
    _build_m2m_conditions,
    _build_m2m_create_sql,
    _build_m2m_drop_sql,
    _build_policy_create_sql,
    _build_policy_drop_sql,
    _build_sharing_policy_create_sql,
    _build_sharing_policy_drop_sql,
    _validate_field_name,
    _validate_guc_name_for_ddl,
    _validate_model_path,
    _validate_pk_type,
)
from django_rls_tenants.rls.policy_sql import bool_flag_sql, tenant_value_sql
from django_rls_tenants.rls.predicates import _predicates_sql, _validated_predicates

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import ProjectState

    from django_rls_tenants.rls.predicates import Predicate


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
    # The "+" prefix in the makemigrations-style listing (formatted_description()).
    category = OperationCategory.ADDITION

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
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,  # noqa: ARG002
        to_state: ProjectState,
    ) -> None:
        """Create the M2M RLS policy, on a database the router lets ``app_label`` migrate to."""
        if not router.allow_migrate(schema_editor.connection.alias, app_label):
            return
        table = self.m2m_table
        admin_check = bool_flag_sql(self.guc_admin_var)
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
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,  # noqa: ARG002
        to_state: ProjectState,  # noqa: ARG002
    ) -> None:
        """Drop the M2M RLS policy and disable RLS, where the router allows it."""
        if not router.allow_migrate(schema_editor.connection.alias, app_label):
            return
        schema_editor.execute(_build_m2m_drop_sql(table=self.m2m_table))

    def describe(self) -> str:
        """Return a human-readable description."""
        return f"Add M2M RLS policy to {self.m2m_table}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Return args for Django's migration serializer.

        The name is the bare class name, as Django's own operations return it:
        ``MigrationWriter`` writes ``<module>.<name>(`` and imports the module
        itself, so a dotted path would be written twice (#46).
        """
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
        return (self.__class__.__name__, [], kwargs)


class AddRLSPolicy(migrations.operations.base.Operation):
    """Migration operation to add the tenant RLS policy to a model's table.

    Creates the policy ``RLSConstraint`` creates (the same SQL, from the same
    builder), for a model that cannot carry the constraint: one in a
    third-party app, whose ``Meta`` and migrations the project does not own.
    The project adds the operation to a migration of its own app and points
    ``app_label`` at the model's app; that migration must depend on the
    migration of that app that creates the tenant foreign key. The table must
    already have the column: the operation adds none.

    To make the library's runtime see the table (``check_rls``, the
    cross-tenant foreign key validation, the ``select_related()`` and
    ``prefetch_related()`` tenant filters, system check ``W012``), list the
    model in ``RLS_TENANTS["EXTERNAL_MODELS"]`` as well.

    The operation is reversible: ``database_backwards`` drops the policy and
    disables RLS on the table. It changes no model state. Like Django's own
    operations, it does nothing on a database the router does not let the
    model's app migrate to, nor for an unmanaged or swapped-out model. It
    does nothing either, in both directions, while the model has an
    ``RLSConstraint`` in the migration state (the app adopted one): the
    constraint's policy has the same name, and dropping it would leave the
    table unprotected.

    Args:
        model_name: The model's name (``"Book"``).
        field: The tenant foreign key of the model; the policy compares its
            column with the current tenant. Default: ``"tenant"``.
        app_label: The model's app, when it is not the app of the migration
            the operation is in. Default: the migration's app.
        tenant_pk_type: SQL cast type for the tenant PK. Default:
            ``RLS_TENANTS["TENANT_PK_TYPE"]`` when the migration is applied.
        guc_tenant_var: GUC variable holding the current tenant ID. Default:
            the one ``RLS_TENANTS["GUC_PREFIX"]`` derives when the migration
            is applied (``"rls.current_tenant"``).
        guc_admin_var: GUC variable for admin bypass. Default: the one
            ``RLS_TENANTS["GUC_PREFIX"]`` derives when the migration is
            applied (``"rls.is_admin"``).
        extra_bypass_flags: Additional GUC variables that bypass the
            ``USING`` clause (but not ``WITH CHECK``), as on ``RLSConstraint``.
        extra_predicates: Rows of other tenants the current tenant may read,
            in a second, ``FOR SELECT`` policy, as on ``RLSConstraint``. A
            ``SharedWith``'s ``through`` model must be in the migration state
            when the operation runs (a dependency, or an earlier operation).

    Raises:
        ValueError: If an argument contains characters that cannot be
            interpolated into the policy SQL safely.
        TypeError: If an item of ``extra_predicates`` is not a predicate.
    """

    reduces_to_sql = True
    reversible = True
    # The "+" prefix in the makemigrations-style listing (formatted_description()).
    category = OperationCategory.ADDITION

    def __init__(
        self,
        model_name: str,
        *,
        field: str = "tenant",
        app_label: str | None = None,
        tenant_pk_type: str | None = None,
        guc_tenant_var: str | None = None,
        guc_admin_var: str | None = None,
        extra_bypass_flags: list[str] | None = None,
        extra_predicates: Sequence[Predicate] | None = None,
    ) -> None:
        _validate_field_name(model_name, "model_name")
        _validate_field_name(field, "field")
        if app_label is not None:
            _validate_field_name(app_label, "app_label")
        if tenant_pk_type is not None:
            _validate_pk_type(tenant_pk_type)
        if guc_tenant_var is not None:
            _validate_guc_name_for_ddl(guc_tenant_var, "guc_tenant_var")
        if guc_admin_var is not None:
            _validate_guc_name_for_ddl(guc_admin_var, "guc_admin_var")
        for flag in extra_bypass_flags or []:
            _validate_guc_name_for_ddl(flag, "extra_bypass_flags")
        self.model_name = model_name
        self.field = field
        self.app_label = app_label
        self.tenant_pk_type = tenant_pk_type
        self.guc_tenant_var = guc_tenant_var
        self.guc_admin_var = guc_admin_var
        self.extra_bypass_flags = list(extra_bypass_flags or [])
        self.extra_predicates = _validated_predicates(extra_predicates)

    @property
    def model_name_lower(self) -> str:
        """The model name, lower-cased, as Django's model operations expose it."""
        return self.model_name.lower()

    def _model(self, app_label: str, state: ProjectState) -> type[models.Model]:
        """Return the model from ``state``, from ``self.app_label`` or else ``app_label``.

        Raises:
            ValueError: If the state holds no such model, or it is a proxy.
        """
        target_app = self.app_label or app_label
        try:
            model: type[models.Model] = state.apps.get_model(target_app, self.model_name)
        except LookupError as exc:
            msg = (
                f"AddRLSPolicy: the migration state has no model "
                f"{target_app}.{self.model_name}. Add the migration of {target_app!r} "
                f"that creates it (and its tenant foreign key) to this migration's "
                f"dependencies, and check model_name and app_label."
            )
            raise ValueError(msg) from exc
        if model._meta.proxy:  # noqa: SLF001
            concrete = model._meta.concrete_model._meta.label  # type: ignore[union-attr]  # noqa: SLF001
            msg = (
                f"AddRLSPolicy: {target_app}.{self.model_name} is a proxy model, which has "
                f"no table of its own. Add the policy to its concrete model, {concrete}."
            )
            raise ValueError(msg)
        return model

    def _applies_to(self, alias: str, model: type[models.Model]) -> bool:
        """Whether to create or drop the policy of ``model``'s table on database ``alias``.

        Not where the router (or the model's kind) excludes it, as for Django's
        own operations, and not while the model has an ``RLSConstraint``, which
        owns a policy of the same name.
        """
        if not self.allow_migrate_model(alias, model):
            return False
        constraints = model._meta.constraints  # noqa: SLF001
        return not any(isinstance(constraint, RLSConstraint) for constraint in constraints)

    @staticmethod
    def _table(model: type[models.Model]) -> str:
        """Return ``model``'s ``db_table``, refused if the policy SQL cannot hold it.

        Raises:
            ValueError: If the name contains a quote, a backslash or a dollar
                sign (the ``'schema"."table'`` idiom among them), which would
                break the quoting of the policy SQL.
        """
        table: str = model._meta.db_table  # noqa: SLF001
        if any(char in table for char in "\"'\\$\x00"):
            msg = (
                f"AddRLSPolicy: the table name {table!r} of {model._meta.label} contains a "  # noqa: SLF001
                f"quote, a backslash or a dollar sign, which the policy SQL cannot hold. A "
                f"table in another schema ('schema\".\"table') is not supported."
            )
            raise ValueError(msg)
        return table

    def _column(self, model: type[models.Model]) -> str:
        """Return the SQL reference to the tenant foreign key's column of ``model``.

        The column ``<field>_id`` that Django names a foreign key's column is
        left unquoted when it is lower case, so the text is the same as
        ``RLSConstraint``'s; any other column (a ``db_column``, which may be a
        reserved word, or a name with an upper-case letter, which PostgreSQL
        would fold) is quoted.

        Raises:
            ValueError: If ``model`` has no such foreign key, or its column
                cannot be interpolated into the policy SQL safely.
        """
        label = model._meta.label  # noqa: SLF001
        try:
            field = model._meta.get_field(self.field)  # noqa: SLF001
        except FieldDoesNotExist as exc:
            msg = (
                f"AddRLSPolicy: {label} has no field {self.field!r} in the migration "
                f"state. Pass the tenant foreign key's name as field=, and add the "
                f"migration that creates the field to this migration's dependencies: "
                f"the operation adds no column."
            )
            raise ValueError(msg) from exc
        if not isinstance(field, models.ForeignKey):
            msg = (
                f"AddRLSPolicy: {label}.{self.field} is a {type(field).__name__}, not a "
                f"ForeignKey. Pass the tenant foreign key's name as field=."
            )
            raise ValueError(msg)  # noqa: TRY004  -- a wrong field= argument, like the others
        self._check_target(field, label)
        column: str = field.column
        _validate_field_name(column, f"the column of {label}.{self.field}")
        if column == f"{self.field}_id" and column == column.lower():
            return column
        return f'"{column}"'

    def _check_target(self, field: models.ForeignKey[Any, Any], label: str) -> None:
        """Refuse a tenant foreign key that does not point at ``TENANT_MODEL``.

        The policy would compare another model's key with the tenant GUC and
        ``check_rls`` would report the table as protected (the policy has the
        expected name), as system check ``W016`` says of a listed model.

        Raises:
            ValueError: If ``field`` targets another model than
                ``RLS_TENANTS["TENANT_MODEL"]``.
        """
        # Read only when the migration runs; see _policy_options().
        from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

        tenant_model = rls_tenants_config.TENANT_MODEL
        target = field.related_model
        target_meta = getattr(target, "_meta", None)
        target_label = target_meta.label_lower if target_meta is not None else str(target)
        if target_label.lower() != tenant_model.lower():
            msg = (
                f"AddRLSPolicy: {label}.{self.field} is a foreign key to {target_label}, "
                f"not to RLS_TENANTS['TENANT_MODEL'] ({tenant_model}). Pass the foreign "
                f"key to the tenant model as field=."
            )
            raise ValueError(msg)

    def _policy_options(self) -> tuple[str, str, str]:
        """Return ``(guc_tenant_var, guc_admin_var, tenant_pk_type)``, unset ones from settings.

        Raises:
            ValueError: If a value read from the settings cannot be
                interpolated into the policy SQL safely.
        """
        # The settings are read only when the migration runs; the tenants
        # package imports models, which needs the app registry.
        from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

        conf = rls_tenants_config
        guc_tenant_var = self.guc_tenant_var or conf.GUC_CURRENT_TENANT
        guc_admin_var = self.guc_admin_var or conf.GUC_IS_ADMIN
        tenant_pk_type = self.tenant_pk_type or conf.TENANT_PK_TYPE
        _validate_guc_name_for_ddl(guc_tenant_var, "guc_tenant_var")
        _validate_guc_name_for_ddl(guc_admin_var, "guc_admin_var")
        _validate_pk_type(tenant_pk_type)
        return guc_tenant_var, guc_admin_var, tenant_pk_type

    def state_forwards(
        self,
        app_label: str,
        state: ProjectState,
    ) -> None:
        """No model state changes -- this is a database-only operation."""

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,  # noqa: ARG002
        to_state: ProjectState,
    ) -> None:
        """Enable RLS on the model's table and create its tenant policy.

        With ``extra_predicates``, create its sharing policy too.

        Raises:
            ValueError: If the model or its tenant foreign key is not in the
                migration state, a predicate cannot be built, or a value
                cannot be interpolated safely.
        """
        model = self._model(app_label, to_state)
        if not self._applies_to(schema_editor.connection.alias, model):
            return
        guc_tenant_var, guc_admin_var, tenant_pk_type = self._policy_options()
        table = self._table(model)
        sql = _build_policy_create_sql(
            table=table,
            column=self._column(model),
            guc_tenant_var=guc_tenant_var,
            guc_admin_var=guc_admin_var,
            tenant_pk_type=tenant_pk_type,
            extra_bypass_flags=self.extra_bypass_flags,
        )
        if self.extra_predicates:
            # Built before anything runs, so a predicate that cannot be built
            # leaves the table as it was.
            sql += _build_sharing_policy_create_sql(
                table=table,
                predicates=_predicates_sql(
                    model,
                    self.extra_predicates,
                    tenant_value=tenant_value_sql(guc_tenant_var, tenant_pk_type),
                ),
            )
        schema_editor.execute(sql)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,  # noqa: ARG002
        to_state: ProjectState,
    ) -> None:
        """Drop the tenant policy (and the sharing policy) and disable RLS, where it applies."""
        model = self._model(app_label, to_state)
        if not self._applies_to(schema_editor.connection.alias, model):
            return
        table = self._table(model)
        sql = _build_policy_drop_sql(table=table)
        if self.extra_predicates:
            sql = _build_sharing_policy_drop_sql(table=table) + sql
        schema_editor.execute(sql)

    def describe(self) -> str:
        """Return a human-readable description."""
        model = f"{self.app_label}.{self.model_name}" if self.app_label else self.model_name
        return f"Add RLS policy to {model}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Return args for Django's migration serializer.

        Only the arguments that were given are written, so an unset one keeps
        reading its default (the settings, for the GUC names and the tenant PK
        type) when the migration runs. The name is the bare class name, as
        for ``AddM2MRLSPolicy``.
        """
        kwargs: dict[str, Any] = {"model_name": self.model_name}
        if self.field != "tenant":
            kwargs["field"] = self.field
        if self.app_label is not None:
            kwargs["app_label"] = self.app_label
        if self.tenant_pk_type is not None:
            kwargs["tenant_pk_type"] = self.tenant_pk_type
        if self.guc_tenant_var is not None:
            kwargs["guc_tenant_var"] = self.guc_tenant_var
        if self.guc_admin_var is not None:
            kwargs["guc_admin_var"] = self.guc_admin_var
        if self.extra_bypass_flags:
            kwargs["extra_bypass_flags"] = self.extra_bypass_flags
        if self.extra_predicates:
            kwargs["extra_predicates"] = self.extra_predicates
        return (self.__class__.__name__, [], kwargs)
