"""Django system checks for RLS tenant configuration.

Detects configuration issues at startup that would otherwise cause
silent failures at runtime:

- Superuser database connections (RLS is completely bypassed — warning).
- ``GUC_PREFIX`` mismatch between runtime config and ``RLSConstraint`` defaults.
- ``USE_LOCAL_SET=True`` without ``ATOMIC_REQUESTS=True``.
- ``CONN_MAX_AGE > 0`` with ``USE_LOCAL_SET=False`` (connection-pool GUC leak risk).
- ``DATABASES`` contains aliases not in ``settings.DATABASES``.
- ``USE_LOCAL_SET=True`` without ``ATOMIC_REQUESTS`` on configured databases.
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
    errors.extend(_check_guc_prefix_mismatch())
    errors.extend(_check_use_local_set_requires_atomic())
    errors.extend(_check_conn_max_age_with_session_gucs())
    errors.extend(_check_databases_alias_exists())
    errors.extend(_check_databases_atomic_requests())
    return errors


def _check_superuser_connection() -> list[CheckWarning]:
    """Warn if the default database connection uses a PostgreSQL superuser.

    PostgreSQL superusers bypass ALL Row-Level Security policies, even
    with ``FORCE ROW LEVEL SECURITY``. If the Django application connects
    as a superuser, tenant isolation is completely disabled -- every query
    returns all rows regardless of GUC settings.
    """
    warnings: list[CheckWarning] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
            row = cursor.fetchone()
            if row is not None and row[0]:
                warnings.append(
                    CheckWarning(
                        "The default database connection uses a PostgreSQL "
                        "superuser. Superusers bypass ALL Row-Level Security "
                        "policies, completely disabling tenant isolation.",
                        hint=(
                            "Create a non-superuser role for the Django "
                            "application and set it in DATABASES['default']. "
                            "Example: CREATE ROLE app LOGIN PASSWORD 'secret'; "
                            "GRANT ALL ON ALL TABLES IN SCHEMA public TO app;"
                        ),
                        id="django_rls_tenants.W005",
                    )
                )
    except Exception:
        # Database may not be available at check time (e.g., during
        # collectstatic or other commands that don't need a DB).
        # Skip the check silently -- it will be caught at runtime.
        logger.debug(
            "Could not check superuser status (database may be unavailable).",
            exc_info=True,
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
                            f"Either pass guc_tenant_var={runtime_tenant_guc!r} "
                            f"to RLSConstraint or change GUC_PREFIX to match."
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
                            f"Either pass guc_admin_var={runtime_admin_guc!r} "
                            f"to RLSConstraint or change GUC_PREFIX to match."
                        ),
                        id="django_rls_tenants.W002",
                    )
                )
    return warnings


def _check_use_local_set_requires_atomic() -> list[CheckWarning]:
    """Warn if ``USE_LOCAL_SET=True`` without ``ATOMIC_REQUESTS=True``.

    ``SET LOCAL`` requires ``transaction.atomic()``. Without
    ``ATOMIC_REQUESTS=True``, the middleware does not wrap requests in
    transactions, causing runtime crashes.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if not rls_tenants_config.USE_LOCAL_SET:
        return warnings

    databases = getattr(settings, "DATABASES", {})
    default_db = databases.get("default", {})
    if not default_db.get("ATOMIC_REQUESTS", False):
        warnings.append(
            CheckWarning(
                "USE_LOCAL_SET=True requires ATOMIC_REQUESTS=True in "
                "DATABASES['default']. SET LOCAL only works inside a "
                "transaction. Without ATOMIC_REQUESTS, the middleware "
                "will crash at runtime.",
                hint="Set DATABASES['default']['ATOMIC_REQUESTS'] = True.",
                id="django_rls_tenants.W003",
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
                    f"USE_LOCAL_SET=True with ATOMIC_REQUESTS=True for safer "
                    f"connection pooling.",
                    hint=f"Set USE_LOCAL_SET=True and ATOMIC_REQUESTS=True, "
                    f"or set DATABASES[{alias!r}]['CONN_MAX_AGE'] = 0.",
                    id="django_rls_tenants.W004",
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


def _check_databases_atomic_requests() -> list[CheckWarning]:
    """Warn if ``USE_LOCAL_SET=True`` but ``ATOMIC_REQUESTS`` is not enabled.

    Checks all aliases in ``RLS_TENANTS["DATABASES"]``, not just
    ``default``. ``SET LOCAL`` requires an active transaction on each
    database.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if not rls_tenants_config.USE_LOCAL_SET:
        return warnings

    django_databases = getattr(settings, "DATABASES", {})
    for alias in rls_tenants_config.DATABASES:
        db_config = django_databases.get(alias, {})
        if not db_config.get("ATOMIC_REQUESTS", False):
            warnings.append(
                CheckWarning(
                    f"USE_LOCAL_SET=True but DATABASES[{alias!r}] does not "
                    f"have ATOMIC_REQUESTS=True. SET LOCAL requires an active "
                    f"transaction. The middleware will crash at runtime when "
                    f"setting GUCs on {alias!r}.",
                    hint=f"Set DATABASES[{alias!r}]['ATOMIC_REQUESTS'] = True.",
                    id="django_rls_tenants.W007",
                )
            )
    return warnings
