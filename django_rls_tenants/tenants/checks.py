"""Django system checks for RLS tenant configuration.

Detects configuration issues at startup that would otherwise cause
silent failures at runtime:

- ``GUC_PREFIX`` mismatch between runtime config and ``RLSConstraint`` defaults.
- ``USE_LOCAL_SET=True`` without ``ATOMIC_REQUESTS=True``.
- ``CONN_MAX_AGE > 0`` with ``USE_LOCAL_SET=False`` (connection-pool GUC leak risk).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.checks import Warning as CheckWarning
from django.core.checks import register

if TYPE_CHECKING:
    from collections.abc import Sequence


@register("django_rls_tenants")
def check_rls_config(
    app_configs: Sequence[Any] | None = None,  # noqa: ARG001  -- Django check API
    **kwargs: Any,  # noqa: ARG001  -- Django check API
) -> list[CheckWarning]:
    """Run all RLS configuration checks."""
    errors: list[CheckWarning] = []
    errors.extend(_check_guc_prefix_mismatch())
    errors.extend(_check_use_local_set_requires_atomic())
    errors.extend(_check_conn_max_age_with_session_gucs())
    return errors


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

    Persistent connections with session-scoped GUCs risk stale tenant
    context leaking between requests if cleanup fails.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if rls_tenants_config.USE_LOCAL_SET:
        return warnings

    databases = getattr(settings, "DATABASES", {})
    default_db = databases.get("default", {})
    conn_max_age = default_db.get("CONN_MAX_AGE", 0)
    if conn_max_age and conn_max_age > 0:
        warnings.append(
            CheckWarning(
                f"CONN_MAX_AGE={conn_max_age} with USE_LOCAL_SET=False "
                f"risks GUC state leaking between requests on persistent "
                f"connections. Consider USE_LOCAL_SET=True with "
                f"ATOMIC_REQUESTS=True for safer connection pooling.",
                hint="Set USE_LOCAL_SET=True and ATOMIC_REQUESTS=True.",
                id="django_rls_tenants.W004",
            )
        )
    return warnings
