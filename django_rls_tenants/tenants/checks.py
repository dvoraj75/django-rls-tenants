"""Django system checks for RLS tenant configuration.

Detects configuration issues at startup that would otherwise cause
silent failures at runtime:

- Superuser or ``BYPASSRLS`` database connections (RLS is completely bypassed — warning).
- A database- or role-level default (``ALTER DATABASE/ROLE ... SET``) for a GUC the
  RLS policies read, in force in every new session before any context is set.
- ``GUC_PREFIX`` mismatch between runtime config and ``RLSConstraint`` defaults.
- ``CONN_MAX_AGE > 0`` with ``USE_LOCAL_SET=False`` (connection-pool GUC leak risk).
- Django's connection pool with ``USE_LOCAL_SET=False`` and no pool ``reset``.
- ``DATABASES`` contains aliases not in ``settings.DATABASES``.
- ``TENANT_MODEL`` does not resolve to an installed model.
- ``TENANT_FK_FIELD`` missing on an ``RLSProtectedModel`` subclass.

Check IDs are never renumbered. ``W003`` (retired in 1.3.1) and ``W007``
(retired in 1.3.2) are no longer emitted: ``RLSTenantMiddleware`` opens its
own transaction when ``USE_LOCAL_SET=True`` (and since 1.4.0 the GUC affinity
guard gives each statement run in autocommit inside a context a transaction of
its own in that mode), so ``ATOMIC_REQUESTS`` is no longer a requirement of
that mode.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.core.checks import Warning as CheckWarning
from django.core.checks import register
from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("django_rls_tenants")


@register("django_rls_tenants")
def check_rls_config(
    app_configs: Sequence[Any] | None = None,  # noqa: ARG001  -- Django check API
    **kwargs: Any,  # noqa: ARG001  -- Django check API
) -> list[CheckWarning]:
    """Run all RLS configuration checks."""
    errors: list[CheckWarning] = []
    errors.extend(_check_superuser_connection())
    errors.extend(_check_policy_guc_defaults())
    errors.extend(_check_guc_prefix_mismatch())
    errors.extend(_check_conn_max_age_with_session_gucs())
    errors.extend(_check_pool_without_reset())
    errors.extend(_check_databases_alias_exists())
    errors.extend(_check_tenant_model_resolves())
    errors.extend(_check_tenant_fk_field_exists())
    return errors


def _check_superuser_connection() -> list[CheckWarning]:
    """Warn if the default database connection uses a role that bypasses RLS.

    Two role attributes make PostgreSQL skip Row-Level Security entirely,
    even with ``FORCE ROW LEVEL SECURITY``:

    - ``SUPERUSER`` -- superusers bypass every policy.
    - ``BYPASSRLS`` -- a non-superuser role with this attribute bypasses
      every policy exactly like a superuser.

    If the Django application connects as such a role, tenant isolation is
    completely disabled -- every query returns all rows regardless of GUC
    settings. Both attributes are read from ``pg_roles`` (which, unlike
    ``pg_user``, also lists ``NOLOGIN`` roles reached via ``SET ROLE``).
    """
    warnings: list[CheckWarning] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            row = cursor.fetchone()
            if row is not None:
                role, is_superuser, bypasses_rls = row
                reasons: list[str] = []
                if is_superuser:
                    reasons.append("is a PostgreSQL superuser")
                if bypasses_rls:
                    reasons.append("has the BYPASSRLS attribute")
                if reasons:
                    quoted_role = connection.ops.quote_name(role)
                    warnings.append(
                        CheckWarning(
                            f"The default database connection role {role!r} "
                            f"{' and '.join(reasons)}. Superusers and BYPASSRLS "
                            "roles bypass ALL Row-Level Security policies, "
                            "completely disabling tenant isolation.",
                            hint=(
                                "Connect Django as a role with NOSUPERUSER and "
                                "NOBYPASSRLS and set it in DATABASES['default']. "
                                "Example: CREATE ROLE app LOGIN PASSWORD 'secret' "
                                "NOSUPERUSER NOBYPASSRLS; GRANT ALL ON ALL TABLES "
                                "IN SCHEMA public TO app; -- or fix the existing "
                                f"role: ALTER ROLE {quoted_role} NOSUPERUSER NOBYPASSRLS;"
                            ),
                            id="django_rls_tenants.W005",
                        )
                    )
    except Exception:
        # Database may not be available at check time (e.g., during
        # collectstatic or other commands that don't need a DB).
        # Skip the check silently -- it will be caught at runtime.
        logger.debug(
            "Could not check RLS-bypass role attributes (database may be unavailable).",
            exc_info=True,
        )
    return warnings


def _check_policy_guc_defaults() -> list[CheckWarning]:
    """Warn if PostgreSQL applies a default value for a GUC the RLS policies read.

    ``ALTER DATABASE ... SET rls.is_admin = 'true'`` or ``ALTER ROLE ... SET``
    makes PostgreSQL set that value at the start of every session of that
    database or role. The policies then read it before any RLS context is set:
    an admin default bypasses them in every session, and a tenant default
    shows that tenant's rows to everyone. The library assumes a new session
    holds no value for these GUCs (the GUC affinity guard clears a session's
    own values only after a context has used it), so such a default is never
    cleared. Reads ``pg_db_role_setting`` on the default connection, for the
    connected database and for cluster-wide (role-only) defaults: role-only
    defaults first, then the database's, then those of a role in the database.
    """
    from django_rls_tenants.pool import _policy_guc_names  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    names = {name.lower() for name in _policy_guc_names()}
    if not names:
        return warnings
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.datname, r.rolname, s.setconfig "
                "FROM pg_db_role_setting AS s "
                "LEFT JOIN pg_database AS d ON d.oid = s.setdatabase "
                "LEFT JOIN pg_roles AS r ON r.oid = s.setrole "
                "WHERE s.setdatabase = 0 OR d.datname = current_database() "
                "ORDER BY d.datname NULLS FIRST, r.rolname NULLS FIRST"
            )
            rows = cursor.fetchall()
    except Exception:
        # Database may not be available at check time; see _check_superuser_connection().
        logger.debug(
            "Could not check for database or role defaults of the RLS GUCs "
            "(database may be unavailable).",
            exc_info=True,
        )
        return warnings
    quote = connection.ops.quote_name
    for database, role, setconfig in rows:
        for entry in setconfig or ():
            name, _, value = entry.partition("=")
            if name.lower() not in names:
                continue
            if role is None:
                scope = f"database {database!r}"
                target = f"DATABASE {quote(database)}"
            elif database is None:
                scope = f"role {role!r} in every database"
                target = f"ROLE {quote(role)}"
            else:
                scope = f"role {role!r} in database {database!r}"
                target = f"ROLE {quote(role)} IN DATABASE {quote(database)}"
            warnings.append(
                CheckWarning(
                    f"PostgreSQL sets {name} = {value!r} at the start of every session "
                    f"for {scope}. The RLS policies read this GUC, so every session "
                    f"starts with that value in force instead of no RLS context "
                    f"(fail-closed), and django-rls-tenants never clears it: an admin "
                    f"value bypasses the policies, a tenant value shows that tenant's "
                    f"rows to everyone.",
                    hint=(
                        f"Remove the default: ALTER {target} RESET {name}; "
                        f"and set the RLS context per request or block instead."
                    ),
                    id="django_rls_tenants.W011",
                )
            )
    return warnings


def _check_guc_prefix_mismatch() -> list[CheckWarning]:
    """Warn if ``GUC_PREFIX`` differs from the ``RLSConstraint`` defaults.

    If the user changes ``GUC_PREFIX`` (e.g., to ``"myapp"``), the middleware
    will set ``myapp.current_tenant`` while the database policy still checks
    ``rls.current_tenant``. Tenant isolation silently breaks.
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.rls.constraints import RLSConstraint  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.models import RLSProtectedModel  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    runtime_tenant_guc = rls_tenants_config.GUC_CURRENT_TENANT
    runtime_admin_guc = rls_tenants_config.GUC_IS_ADMIN

    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model._meta.abstract:  # noqa: SLF001
            continue
        for constraint in model._meta.constraints:  # noqa: SLF001
            if not isinstance(constraint, RLSConstraint):
                continue
            if constraint.guc_tenant_var != runtime_tenant_guc:
                warnings.append(
                    CheckWarning(
                        f"RLSConstraint on {model.__name__} uses "
                        f"guc_tenant_var={constraint.guc_tenant_var!r} but "
                        f"GUC_PREFIX derives {runtime_tenant_guc!r}. "
                        f"Tenant isolation will silently break.",
                        hint=(
                            f"Either pass guc_tenant_var={runtime_tenant_guc!r} to "
                            f"the RLSConstraint, or set RLS_TENANTS['GUC_PREFIX'] so "
                            f"it derives {runtime_tenant_guc!r}."
                        ),
                        id="django_rls_tenants.W001",
                    )
                )
            if constraint.guc_admin_var != runtime_admin_guc:
                warnings.append(
                    CheckWarning(
                        f"RLSConstraint on {model.__name__} uses "
                        f"guc_admin_var={constraint.guc_admin_var!r} but "
                        f"GUC_PREFIX derives {runtime_admin_guc!r}. "
                        f"Admin bypass will silently break.",
                        hint=(
                            f"Either pass guc_admin_var={runtime_admin_guc!r} to "
                            f"the RLSConstraint, or set RLS_TENANTS['GUC_PREFIX'] so "
                            f"it derives {runtime_admin_guc!r}."
                        ),
                        id="django_rls_tenants.W002",
                    )
                )
    return warnings


def _check_conn_max_age_with_session_gucs() -> list[CheckWarning]:
    """Warn if ``CONN_MAX_AGE > 0`` with ``USE_LOCAL_SET=False``.

    Checks all aliases in ``RLS_TENANTS["DATABASES"]``. Persistent
    connections with session-scoped GUCs risk stale tenant context
    leaking between requests if cleanup fails.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if rls_tenants_config.USE_LOCAL_SET:
        return warnings

    django_databases = getattr(settings, "DATABASES", {})
    for alias in rls_tenants_config.DATABASES:
        db_config = django_databases.get(alias, {})
        conn_max_age = db_config.get("CONN_MAX_AGE", 0)
        # None means "keep connections forever" -- the most dangerous value.
        if conn_max_age is None or (isinstance(conn_max_age, (int, float)) and conn_max_age > 0):
            warnings.append(
                CheckWarning(
                    f"DATABASES[{alias!r}] has CONN_MAX_AGE={conn_max_age} "
                    f"with USE_LOCAL_SET=False. This risks GUC state leaking "
                    f"between requests on persistent connections. Consider "
                    f"USE_LOCAL_SET=True, which scopes the GUCs to "
                    f"transactions, so they never stay on a connection.",
                    hint=f"Set RLS_TENANTS['USE_LOCAL_SET'] = True, "
                    f"or set DATABASES[{alias!r}]['CONN_MAX_AGE'] = 0.",
                    id="django_rls_tenants.W004",
                )
            )
    return warnings


def _check_pool_without_reset() -> list[CheckWarning]:
    """Warn if a pooled alias has no ``reset`` callback with ``USE_LOCAL_SET=False``.

    Checks all aliases in ``RLS_TENANTS["DATABASES"]``. Django rejects a
    non-zero ``CONN_MAX_AGE`` with ``OPTIONS["pool"]``, so ``W004`` never fires
    for a working pool, yet a pooled connection keeps its session GUCs across
    requests just like a persistent one. The ``connection_created`` handler
    clears them on the next checkout; a ``reset`` callback also clears them on
    the way back into the pool, so idle connections hold no tenant state.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.pool import _pool_options  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if rls_tenants_config.USE_LOCAL_SET:
        return warnings

    django_databases = getattr(settings, "DATABASES", {})
    for alias in rls_tenants_config.DATABASES:
        pool_options = _pool_options(django_databases.get(alias, {}))
        if pool_options is not None and pool_options.get("reset") is None:
            warnings.append(
                CheckWarning(
                    f"DATABASES[{alias!r}] uses Django's connection pool with "
                    f"USE_LOCAL_SET=False and no pool 'reset' callback. A connection "
                    f"that a request's cleanup did not reach goes back to the pool "
                    f"still holding that request's RLS GUCs, until its next checkout "
                    f"clears them.",
                    hint=(
                        f"Set DATABASES[{alias!r}]['OPTIONS']['pool']['reset'] to "
                        f"django_rls_tenants.pool.reset_rls_gucs, or set "
                        f"RLS_TENANTS['USE_LOCAL_SET'] = True."
                    ),
                    id="django_rls_tenants.W010",
                )
            )
    return warnings


def _check_databases_alias_exists() -> list[CheckWarning]:
    """Warn if ``DATABASES`` contains an alias not in ``settings.DATABASES``.

    Catches typos like ``"replca"`` instead of ``"replica"`` that would
    cause ``set_guc`` to fail at runtime when the middleware tries to
    open a connection to a non-existent database alias.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    django_databases = set(getattr(settings, "DATABASES", {}).keys())
    return [
        CheckWarning(
            f"RLS_TENANTS['DATABASES'] contains {alias!r} which is "
            f"not defined in settings.DATABASES. The middleware will "
            f"fail at runtime when trying to set GUCs on this alias.",
            hint=(
                f"Check for typos. Defined database aliases: "
                f"{', '.join(sorted(django_databases))}."
            ),
            id="django_rls_tenants.W006",
        )
        for alias in rls_tenants_config.DATABASES
        if alias not in django_databases
    ]


def _check_tenant_model_resolves() -> list[CheckWarning]:
    """Warn if ``TENANT_MODEL`` does not resolve to an installed model.

    ``RLS_TENANTS["TENANT_MODEL"]`` must be a ``"<app_label>.<ModelName>"``
    path to a model in ``INSTALLED_APPS``. A typo or an app missing from
    ``INSTALLED_APPS`` otherwise surfaces only at query time -- when the
    tenant FK is resolved -- with a cryptic ``LookupError``.
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.exceptions import RLSConfigurationError  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    try:
        model_path = rls_tenants_config.TENANT_MODEL
    except RLSConfigurationError:
        # TENANT_MODEL is required; its absence is reported when the config
        # is first read elsewhere. Don't crash the check here.
        return warnings

    try:
        apps.get_model(model_path)
    except (LookupError, ValueError):
        # ValueError: malformed path (not "app_label.ModelName").
        # LookupError: unknown app label or model name.
        warnings.append(
            CheckWarning(
                f"RLS_TENANTS['TENANT_MODEL'] = {model_path!r} does not "
                f"resolve to an installed model.",
                hint=(
                    "Set RLS_TENANTS['TENANT_MODEL'] to '<app_label>.<ModelName>' "
                    "naming a model in INSTALLED_APPS."
                ),
                id="django_rls_tenants.W008",
            )
        )
    return warnings


def _check_tenant_fk_field_exists() -> list[CheckWarning]:
    """Warn if a concrete ``RLSProtectedModel`` subclass lacks its tenant field.

    Each model's ``RLSConstraint`` generates an RLS policy that filters on a
    tenant column. The referenced field name comes from the model's
    ``RLSConstraint(field=...)`` when present, falling back to
    ``RLS_TENANTS["TENANT_FK_FIELD"]`` (default ``"tenant"``) -- the same
    resolution order ``register_m2m_rls`` uses.

    If that field is missing -- e.g. a constraint's ``field`` (or
    ``TENANT_FK_FIELD``) names a column no model actually defines -- the
    generated policy references a column that doesn't exist, failing at
    migrate/query time. This catches the mismatch at startup.

    The field name is resolved from the constraint rather than blindly from
    ``TENANT_FK_FIELD``: the ``class_prepared`` injector always adds a field
    named ``TENANT_FK_FIELD``, so checking that name alone would always pass.
    The policy uses the *constraint's* field, so that is what must exist.
    """
    from django.apps import apps  # noqa: PLC0415
    from django.core.exceptions import FieldDoesNotExist  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.models import (  # noqa: PLC0415
        RLSProtectedModel,
        _get_tenant_fk_field,
    )

    warnings: list[CheckWarning] = []
    default_fk_field = rls_tenants_config.TENANT_FK_FIELD
    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model._meta.abstract:  # noqa: SLF001
            continue
        fk_field = _get_tenant_fk_field(model) or default_fk_field
        try:
            model._meta.get_field(fk_field)  # noqa: SLF001
        except FieldDoesNotExist:
            warnings.append(
                CheckWarning(
                    f"{model.__name__} has no field {fk_field!r} referenced by its RLS policy.",
                    hint=(
                        f"Add a {fk_field!r} ForeignKey to {model.__name__}, or "
                        f"point RLS_TENANTS['TENANT_FK_FIELD'] (or the constraint's "
                        f"field=) at an existing field."
                    ),
                    id="django_rls_tenants.W009",
                    obj=model,
                )
            )
    return warnings
