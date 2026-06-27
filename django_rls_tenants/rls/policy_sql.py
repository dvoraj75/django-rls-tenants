"""Single source of truth for RLS policy-predicate SQL fragments.

Every RLS policy predicate reads PostgreSQL session variables (GUCs) with
``current_setting()``.  These helpers build those fragments in one place so that
``RLSConstraint`` / ``RLSM2MConstraint`` (this layer), the ``AddM2MRLSPolicy``
migration operation, and the ``setup_m2m_rls`` command all emit byte-for-byte
identical predicates.

Each ``current_setting()`` read is wrapped in an uncorrelated scalar sub-SELECT,
``(SELECT current_setting('<guc>', true))``.  PostgreSQL hoists such a sub-SELECT
into a once-per-statement *InitPlan* instead of re-reading the GUC for every row
scanned -- a free win on large, admin, and raw-SQL scans.  The predicate is
otherwise unchanged, so query semantics are identical to the inline form.

This module lives in the ``rls`` layer and imports nothing from ``tenants`` (a
boundary enforced by ``tests/test_layering.py``).

Safety:
    These helpers perform string interpolation only; they add no escaping.
    Callers must validate ``guc_var`` / ``column`` / ``tenant_pk_type`` against
    the allowlists in :mod:`django_rls_tenants.rls.constraints` (GUC names against
    ``_GUC_NAME_RE``, PK types against ``_ALLOWED_PK_TYPES``, columns against
    ``_FIELD_NAME_RE``) before passing them here.
"""

from __future__ import annotations


def scalar_setting(guc_var: str) -> str:
    """Return ``(SELECT current_setting('<guc>', true))`` for ``guc_var``.

    Wrapping the read in an uncorrelated scalar sub-SELECT lets PostgreSQL hoist
    it into a once-per-statement InitPlan rather than evaluating ``current_setting``
    per row.  The ``true`` (``missing_ok``) argument makes an unset GUC return an
    empty string instead of raising.

    Args:
        guc_var: GUC variable name (e.g. ``"rls.current_tenant"``).

    Returns:
        The scalar sub-SELECT SQL fragment.
    """
    return f"(SELECT current_setting('{guc_var}', true))"


def tenant_value_sql(guc_tenant_var: str, tenant_pk_type: str) -> str:
    """Return the current-tenant GUC read, cast to the tenant PK type.

    Produces ``nullif((SELECT current_setting('<guc>', true)), '')::<type>`` --
    the right-hand side of the tenant equality, also usable on its own in an
    ``INSERT`` / ``SELECT`` value list.  ``nullif(..., '')`` maps an unset GUC to
    ``NULL`` so the cast never fails on an empty string.

    Args:
        guc_tenant_var: GUC variable holding the current tenant ID.
        tenant_pk_type: SQL cast type for the tenant PK (e.g. ``"int"``).

    Returns:
        The cast tenant-value SQL fragment.
    """
    return f"nullif({scalar_setting(guc_tenant_var)}, '')::{tenant_pk_type}"


def tenant_match_sql(column: str, guc_tenant_var: str, tenant_pk_type: str) -> str:
    """Return a ``<column> = <current tenant>`` equality predicate.

    Compares ``column`` against the current-tenant GUC cast to the tenant PK
    type.  An unset GUC becomes ``NULL`` (via :func:`tenant_value_sql`), so the
    equality yields ``NULL`` -- no rows match -- rather than a cast error.

    Args:
        column: Column reference to compare (e.g. ``"tenant_id"`` or a
            table-qualified ``'"orders".tenant_id'``).
        guc_tenant_var: GUC variable holding the current tenant ID.
        tenant_pk_type: SQL cast type for the tenant PK (e.g. ``"int"``).

    Returns:
        The equality predicate SQL fragment.
    """
    return f"{column} = {tenant_value_sql(guc_tenant_var, tenant_pk_type)}"


def bool_flag_sql(guc_var: str) -> str:
    """Return a ``<guc> = 'true'`` boolean-flag predicate.

    Used for the admin-bypass GUC and any extra bypass flags, each of which
    holds the string ``"true"`` / ``"false"``.

    Args:
        guc_var: GUC variable holding a boolean flag (e.g. ``"rls.is_admin"``).

    Returns:
        The boolean-flag predicate SQL fragment.
    """
    return f"{scalar_setting(guc_var)} = 'true'"
