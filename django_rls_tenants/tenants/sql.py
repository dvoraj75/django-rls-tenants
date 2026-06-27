"""Safe raw-SQL helpers for scoping queries to the current tenant.

Sometimes the ORM is not enough -- a hand-written report, a bulk ``UPDATE``, or
a query against a view -- yet the query must still respect tenant isolation.
RLS already enforces isolation at the database level, but adding the *same*
predicate to a raw query makes the intent explicit, lets the planner use a
tenant index, and keeps admin-bypass behaviour identical to the live policies.

:func:`safe_tenant_sql` returns a ``WHERE``-clause fragment and
:func:`current_tenant_value_sql` returns the current-tenant value expression.
Both are built from the exact same :mod:`~django_rls_tenants.rls.policy_sql`
helpers the RLS policies use, so the predicate they apply is *semantically
identical* to what PostgreSQL enforces.  (The bare tenant match is byte-for-byte
the policy's; the admin-inclusive form is ``(<match> OR <admin>)`` rather than
the policy's ``CASE WHEN <admin> THEN true ELSE <match> END`` -- a different
spelling of the same truth table, not a different result.)

Safety:
    The returned fragments contain **no bind parameters**.  The tenant id is read
    *inside PostgreSQL* from the session GUC (set by ``tenant_context()`` /
    middleware), so the fragment carries zero Python-side user input.  Every
    identifier interpolated into the fragment -- the column, the optional table,
    the GUC names, and the PK cast type -- is validated against the same
    allowlists the RLS constraints use; an invalid identifier raises
    ``ValueError`` rather than producing injectable SQL.

Example:
    >>> from django_rls_tenants import safe_tenant_sql, tenant_context
    >>> from django.db import connection
    >>> with tenant_context(tenant.pk), connection.cursor() as cursor:
    ...     cursor.execute(
    ...         f"SELECT product, amount FROM orders WHERE {safe_tenant_sql()} "
    ...         f"AND amount > %s",
    ...         [100],
    ...     )
    ...     rows = cursor.fetchall()
"""

from __future__ import annotations

from django_rls_tenants.rls.constraints import _validate_field_name, _validate_pk_type
from django_rls_tenants.rls.guc import _validate_guc_name
from django_rls_tenants.rls.policy_sql import bool_flag_sql, tenant_match_sql, tenant_value_sql
from django_rls_tenants.tenants.conf import rls_tenants_config


def _validated_tenant_guc_and_pk_type() -> tuple[str, str]:
    """Return the validated current-tenant GUC name and tenant PK cast type.

    Reads both from ``RLS_TENANTS`` and validates them against the same
    allowlists the RLS constraints use, raising ``ValueError`` on a bad value.
    Shared by :func:`safe_tenant_sql` and :func:`current_tenant_value_sql` so
    the two helpers never drift in how they resolve and validate config.

    Returns:
        A ``(guc_current_tenant, tenant_pk_type)`` tuple.

    Raises:
        ValueError: If the configured GUC name or tenant PK type is invalid.
    """
    conf = rls_tenants_config
    guc_tenant = conf.GUC_CURRENT_TENANT
    pk_type = conf.TENANT_PK_TYPE
    _validate_guc_name(guc_tenant)
    _validate_pk_type(pk_type)
    return guc_tenant, pk_type


def safe_tenant_sql(
    column: str = "tenant_id",
    *,
    table: str | None = None,
    include_admin: bool = True,
    extra_bypass_flags: list[str] | None = None,
) -> str:
    """Return a ``WHERE``-clause fragment scoping rows to the current tenant.

    The fragment compares ``column`` against the current-tenant GUC, using the
    exact expression the RLS policies use (via
    :mod:`~django_rls_tenants.rls.policy_sql`).  When ``include_admin`` is true
    it also lets rows through while the admin-bypass GUC is set, mirroring
    ``admin_context()``.  Splice it straight into a raw query::

        sql = f"SELECT * FROM orders WHERE {safe_tenant_sql()} AND amount > %s"
        cursor.execute(sql, [100])

    There are deliberately no bind parameters: the tenant id is read inside
    PostgreSQL from the session GUC, so the fragment contains no Python-side
    user input.

    Args:
        column: Tenant foreign-key column on the target table. Defaults to
            ``"tenant_id"`` (Django's column name for a ``tenant`` FK).
        table: Optional table name/alias to qualify the column with, e.g.
            ``safe_tenant_sql(table="orders")`` yields ``"orders".tenant_id``.
            Use it when the query joins several tables and ``column`` would be
            ambiguous.
        include_admin: When ``True`` (default), the fragment also matches every
            row while the admin-bypass GUC is active, so a query run inside
            ``admin_context()`` sees all tenants -- matching the RLS policy. Set
            it to ``False`` to scope strictly to the current tenant regardless
            of admin (or any other bypass) state.
        extra_bypass_flags: Additional boolean bypass GUCs that should also let
            rows through, matching the ``extra_bypass_flags`` you passed to the
            model's :class:`~django_rls_tenants.rls.constraints.RLSConstraint`.
            Pass the *same* list here so the fragment mirrors the live policy's
            ``USING`` clause; otherwise a session with one of those flags set
            passes the policy but is filtered out by this fragment. Only applied
            when ``include_admin=True`` (these flags extend the bypass set);
            ignored when ``include_admin=False``.

    Returns:
        A SQL ``WHERE``-clause fragment. With ``include_admin=True`` it is
        wrapped in parentheses (``(<match> OR <admin> [OR <flag>...])``) so it
        composes safely with surrounding ``AND`` clauses. With
        ``include_admin=False`` it is the bare ``<match>`` predicate.

    Warning:
        The parentheses only make the fragment safe to combine with ``AND``.
        Appending ``OR`` defeats tenant isolation -- ``WHERE {safe_tenant_sql()}
        OR is_public`` returns rows from *every* tenant. Always restrict
        further with ``AND``, never ``OR``.

    Raises:
        ValueError: If ``column`` or ``table`` is not a valid SQL identifier, or
            if the configured GUC names, the tenant PK type, or any
            ``extra_bypass_flags`` entry is invalid.

    Example:
        >>> safe_tenant_sql("tenant_id", include_admin=False)
        "tenant_id = nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"

        The default (``include_admin=True``) wraps that predicate as
        ``(<predicate> OR (SELECT current_setting('rls.is_admin', true)) = 'true')``.
    """
    guc_tenant, pk_type = _validated_tenant_guc_and_pk_type()

    _validate_field_name(column, "column")
    if table is not None:
        _validate_field_name(table, "table")
        col = f'"{table}".{column}'
    else:
        col = column

    predicate = tenant_match_sql(col, guc_tenant, pk_type)
    if not include_admin:
        return predicate

    # Bypass set mirrors RLSConstraint's USING clause: admin first, then any
    # extra flags, each read via the same InitPlan-wrapped boolean-flag helper.
    guc_admin = rls_tenants_config.GUC_IS_ADMIN
    _validate_guc_name(guc_admin)
    bypass_conditions = [bool_flag_sql(guc_admin)]
    for flag in extra_bypass_flags or []:
        _validate_guc_name(flag)
        bypass_conditions.append(bool_flag_sql(flag))

    bypass_clause = " OR ".join(bypass_conditions)
    return f"({predicate} OR {bypass_clause})"


def current_tenant_value_sql() -> str:
    """Return the current-tenant value expression for use in raw SQL.

    Produces the same cast GUC read the RLS policies compare against -- e.g.
    ``nullif((SELECT current_setting('rls.current_tenant', true)), '')::int`` --
    suitable for an ``INSERT`` value list or a ``SELECT`` projection::

        cursor.execute(
            f"INSERT INTO orders (product, tenant_id) VALUES (%s, {current_tenant_value_sql()})",
            ["Widget"],
        )

    An unset GUC evaluates to ``NULL`` (via ``nullif(..., '')``), so the cast
    never fails on an empty string.

    Returns:
        A SQL value expression yielding the current tenant id (or ``NULL`` when
        no tenant context is active).

    Raises:
        ValueError: If the configured GUC name or tenant PK type is invalid.

    Example:
        >>> current_tenant_value_sql()
        "nullif((SELECT current_setting('rls.current_tenant', true)), '')::int"
    """
    guc_tenant, pk_type = _validated_tenant_guc_and_pk_type()
    return tenant_value_sql(guc_tenant, pk_type)
