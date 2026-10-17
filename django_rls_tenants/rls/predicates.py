"""Extra RLS policy predicates: rows a tenant may read beyond its own (#59).

``RLSConstraint(extra_predicates=[...])`` and the ``AddRLSPolicy`` migration
operation take a list of these. They do not touch the tenant policy
(``<table>_tenant_isolation_policy``): the predicates go into a second policy,
``<table>_tenant_sharing_policy``, a permissive ``FOR SELECT`` policy whose
``USING`` ORs them. PostgreSQL ORs the permissive policies of a command, so a
row passes ``SELECT`` when the tenant policy or one of the predicates lets it
through. ``UPDATE``, ``DELETE``, ``INSERT`` and ``SELECT ... FOR UPDATE`` /
``FOR SHARE`` are not ``SELECT`` policies' commands, so they read the tenant
policy alone: a predicate never lets a tenant change, delete or take over a
row of another tenant, and such a row is hidden from ``SELECT ... FOR UPDATE``.

Each predicate builds its SQL from the model the policy is created for (the
migration state's model during ``migrate``) and ``tenant_value``, the
current-tenant GUC cast to the tenant PK type (``NULL`` when no tenant is set,
so a predicate that compares with it matches nothing outside a tenant context).
The predicates are deconstructible, so a migration can hold them.

Safety: identifiers come from the model registry and are double-quoted; a name
containing a quote, a backslash, a dollar sign or a NUL is refused, since it
would break the quoting of the policy SQL. ``RawPredicate`` is SQL the developer
writes; it is checked only for what would break out of the policy statement.

This module lives in the ``rls`` layer and imports nothing from ``tenants``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.utils.deconstruct import deconstructible

from django_rls_tenants.rls.constraints import _validate_field_name, _validate_model_path

if TYPE_CHECKING:
    from collections.abc import Sequence

# Characters an identifier interpolated into the policy SQL must not contain:
# a double quote would end the quoted identifier, and the SQL runs inside a
# DO block's dollar-quoted strings ($$ / $BODY$).
_UNSAFE_IDENTIFIER_CHARS = "\"'\\$\x00"

# Text a RawPredicate must not contain: a statement separator, comments (which
# could hide the rest of the policy statement), a dollar sign (the end of the
# DO block's dollar-quoted strings) and a NUL.
_UNSAFE_RAW_SQL = (";", "--", "/*", "$", "\x00")

# The alias of the through table inside SharedWith's EXISTS subquery, so the
# protected table's own columns stay reachable by its name.
_THROUGH_ALIAS = '"rls_shared"'


def _quoted(name: str, what: str) -> str:
    """Return ``name`` as a double-quoted identifier.

    Raises:
        ValueError: If ``name`` contains a character that would break the
            quoting of the policy SQL.
    """
    if any(char in name for char in _UNSAFE_IDENTIFIER_CHARS):
        msg = (
            f"The {what} {name!r} contains a quote, a backslash, a dollar sign or a NUL, "
            f"which the RLS policy SQL cannot hold."
        )
        raise ValueError(msg)
    return f'"{name}"'


def _foreign_key(model: type[models.Model], name: str, param: str) -> models.ForeignKey[Any, Any]:
    """Return ``model``'s foreign key ``name``.

    Raises:
        ValueError: If ``model`` has no such field, or it is not a foreign key.
    """
    label = model._meta.label  # noqa: SLF001
    try:
        field = model._meta.get_field(name)  # noqa: SLF001
    except FieldDoesNotExist as exc:
        msg = f"{param}={name!r}: {label} has no field {name!r}."
        raise ValueError(msg) from exc
    if not isinstance(field, models.ForeignKey):
        msg = f"{param}={name!r}: {label}.{name} is a {type(field).__name__}, not a ForeignKey."
        raise ValueError(msg)  # noqa: TRY004  -- a wrong argument, like the missing field
    return field


@deconstructible
class Predicate:
    """Base class of the predicates ``extra_predicates`` takes.

    A subclass implements :meth:`policy_sql` and ``_key()`` (the values that
    make two predicates equal). ``@deconstructible`` on this class records the
    arguments each instance is created with, which is what a migration writes,
    so a subclass's ``__init__`` arguments must be serializable.
    """

    def policy_sql(self, model: type[models.Model], *, tenant_value: str) -> str:
        """Return the predicate's SQL for the policy of ``model``'s table.

        Args:
            model: The model whose table the policy is created on.
            tenant_value: SQL expression of the current tenant (the GUC cast
                to the tenant PK type, ``NULL`` when it is unset).

        Raises:
            ValueError: If the predicate cannot be built for ``model``.
        """
        raise NotImplementedError

    def _key(self) -> tuple[Any, ...]:
        """Return the values that make two predicates of one class equal."""
        raise NotImplementedError

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Predicate) and type(other) is type(self):
            return self._key() == other._key()
        return NotImplemented

    def __hash__(self) -> int:
        return hash((type(self), self._key()))

    def __repr__(self) -> str:
        args = ", ".join(repr(value) for value in self._key())
        return f"{type(self).__name__}({args})"


class SharedWith(Predicate):
    """Rows another model's rows share with the current tenant.

    Matches a row when a row of the ``through`` model points at it with its
    ``fk`` foreign key and at the current tenant with its ``tenant_fk``
    foreign key::

        EXISTS (SELECT 1 FROM "<through table>" AS "rls_shared"
                WHERE "rls_shared"."<fk column>" = "<table>"."<target column>"
                AND "rls_shared"."<tenant_fk column>" = <current tenant>)

    The ``through`` model's own RLS policy applies inside the subquery: when
    it is RLS-protected by another column than ``tenant_fk`` (the owner's
    tenant, say), add ``TenantField(tenant_fk)`` to its ``extra_predicates``,
    or its rows stay hidden from the tenant they share with and nothing
    matches (system check ``W018``).

    Args:
        through: Dotted path of the model that records the sharing
            (``"notes.NoteShare"``). In a migration, it must be in the
            migration state when the policy is created: created by an earlier
            migration (a dependency) or by the same one.
        fk: The ``through`` model's foreign key to the protected model.
        tenant_fk: The ``through`` model's foreign key to the tenant a row
            is shared with.

    Raises:
        ValueError: If an argument contains characters that cannot be
            interpolated into the policy SQL safely.
    """

    def __init__(self, *, through: str, fk: str, tenant_fk: str) -> None:
        _validate_model_path(through, "through")
        _validate_field_name(fk, "fk")
        _validate_field_name(tenant_fk, "tenant_fk")
        self.through = through
        self.fk = fk
        self.tenant_fk = tenant_fk

    def _key(self) -> tuple[Any, ...]:
        return (self.through, self.fk, self.tenant_fk)

    def __repr__(self) -> str:
        return (
            f"SharedWith(through={self.through!r}, fk={self.fk!r}, tenant_fk={self.tenant_fk!r})"
        )

    def policy_sql(self, model: type[models.Model], *, tenant_value: str) -> str:
        """Return the ``EXISTS`` subquery on the ``through`` model's table.

        The ``through`` model is looked up in ``model``'s registry: the
        migration state's during ``migrate``.

        Raises:
            ValueError: If the registry has no ``through`` model, ``fk`` is not
                its foreign key to ``model``, or ``tenant_fk`` is not a foreign
                key of it.
        """
        label = model._meta.label  # noqa: SLF001
        try:
            through = model._meta.apps.get_model(self.through)  # noqa: SLF001
        except LookupError as exc:
            msg = (
                f"SharedWith on {label}: there is no model {self.through!r}. In a "
                f"migration, the migration that creates it must run first: add it to "
                f"the dependencies, or create the model in the same migration."
            )
            raise ValueError(msg) from exc
        fk = _foreign_key(through, self.fk, "fk")
        target = fk.related_model
        concrete = target._meta.concrete_model if isinstance(target, type) else None  # noqa: SLF001
        if concrete is None or concrete._meta.label_lower != model._meta.label_lower:  # noqa: SLF001
            shown = target._meta.label if isinstance(target, type) else target  # noqa: SLF001
            msg = (
                f"SharedWith on {label}: fk={self.fk!r} is a foreign key of {self.through} "
                f"to {shown}, not to {label}."
            )
            raise ValueError(msg)
        tenant_fk = _foreign_key(through, self.tenant_fk, "tenant_fk")
        through_table = _quoted(through._meta.db_table, "table name")  # noqa: SLF001
        table = _quoted(model._meta.db_table, "table name")  # noqa: SLF001
        fk_column = _quoted(fk.column, "column name")
        target_field_column = fk.target_field.column
        assert target_field_column is not None  # noqa: S101  -- a foreign key targets a concrete field
        target_column = _quoted(target_field_column, "column name")
        tenant_column = _quoted(tenant_fk.column, "column name")
        return (
            f"EXISTS (SELECT 1 FROM {through_table} AS {_THROUGH_ALIAS} "
            f"WHERE {_THROUGH_ALIAS}.{fk_column} = {table}.{target_column} "
            f"AND {_THROUGH_ALIAS}.{tenant_column} = {tenant_value})"
        )


class TenantField(Predicate):
    """Rows whose foreign key ``field`` points at the current tenant.

    A second tenant column on the row itself: a partner tenant, or the tenant
    a share record is shared with (``TenantField("shared_with")`` on the
    ``through`` model of a :class:`SharedWith`)::

        "<table>"."<field column>" = <current tenant>

    Args:
        field: The foreign key to compare with the current tenant.

    Raises:
        ValueError: If ``field`` contains characters that cannot be
            interpolated into the policy SQL safely.
    """

    def __init__(self, field: str) -> None:
        _validate_field_name(field, "field")
        self.field = field

    def _key(self) -> tuple[Any, ...]:
        return (self.field,)

    def policy_sql(self, model: type[models.Model], *, tenant_value: str) -> str:
        """Return the comparison of the foreign key's column with the current tenant.

        Raises:
            ValueError: If ``model`` has no such foreign key.
        """
        field = _foreign_key(model, self.field, "field")
        table = _quoted(model._meta.db_table, "table name")  # noqa: SLF001
        return f"{table}.{_quoted(field.column, 'column name')} = {tenant_value}"


class RawPredicate(Predicate):
    """A boolean SQL expression the developer writes and owns.

    Used as written, in parentheses. It must compare with the current tenant
    itself, through the GUC the policy reads::

        RawPredicate(
            "partner_id = nullif(current_setting('rls.current_tenant', true), '')::int"
        )

    A predicate that does not depend on the current tenant (``is_public``)
    opens the rows it matches to every tenant, and to sessions with no tenant
    context too. Only text that would break out of the policy statement is
    refused; the expression itself is not checked. Write a ``%`` (a ``LIKE``
    pattern) as in SQL: the policy DDL goes through Django's schema editor,
    which composes it with its (empty) parameters, so :meth:`policy_sql`
    doubles each ``%`` for it.

    Args:
        sql: The SQL boolean expression.

    Raises:
        ValueError: If ``sql`` is empty or contains ``;``, ``--``, ``/*``,
            ``$`` or a NUL.
    """

    def __init__(self, sql: str) -> None:
        if not sql.strip():
            msg = "RawPredicate: the SQL expression is empty."
            raise ValueError(msg)
        for token in _UNSAFE_RAW_SQL:
            if token in sql:
                msg = (
                    f"RawPredicate: the SQL expression {sql!r} contains {token!r}. A "
                    f"predicate cannot hold a statement separator, a comment, a dollar "
                    f"sign or a NUL."
                )
                raise ValueError(msg)
        self.sql = sql

    def _key(self) -> tuple[Any, ...]:
        return (self.sql,)

    def policy_sql(
        self,
        model: type[models.Model],  # noqa: ARG002  -- the Predicate interface
        *,
        tenant_value: str,  # noqa: ARG002  -- the Predicate interface
    ) -> str:
        """Return the expression in parentheses, each ``%`` doubled for the schema editor.

        Django's PostgreSQL schema editor composes every statement with its
        parameters client-side (``%`` is a placeholder there, also with no
        parameters), so a lone ``%`` would fail ``migrate``.
        """
        return f"({self.sql.replace('%', '%%')})"


def _validated_predicates(predicates: Sequence[Any] | None) -> list[Predicate]:
    """Return ``predicates`` as a list, each checked to be a :class:`Predicate`.

    Raises:
        TypeError: If an item is not a ``Predicate``.
    """
    validated = list(predicates or [])
    for predicate in validated:
        if not isinstance(predicate, Predicate):
            msg = (
                f"extra_predicates takes Predicate instances (SharedWith, TenantField, "
                f"RawPredicate), not {predicate!r}."
            )
            raise TypeError(msg)
    return validated


def _predicates_sql(
    model: type[models.Model],
    predicates: Sequence[Predicate],
    *,
    tenant_value: str,
) -> list[str]:
    """Return the SQL of each of ``predicates`` for the policy of ``model``'s table.

    Raises:
        ValueError: If a predicate cannot be built for ``model``.
    """
    return [predicate.policy_sql(model, tenant_value=tenant_value) for predicate in predicates]
