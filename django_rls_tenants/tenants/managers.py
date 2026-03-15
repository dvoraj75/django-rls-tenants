"""Tenant-aware manager and queryset.

``TenantQuerySet`` solves the lazy evaluation problem: it stores the
user reference from ``for_user()`` and sets GUC variables at query
evaluation time (in ``_fetch_all``), not at queryset creation time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import models

from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _resolve_user_guc_vars

if TYPE_CHECKING:
    from django_rls_tenants.tenants.types import TenantUser

logger = logging.getLogger("django_rls_tenants")


class TenantQuerySet(models.QuerySet):  # type: ignore[type-arg]
    """QuerySet that sets RLS GUC variables at evaluation time.

    Stores the user reference from ``for_user()`` and defers GUC setup
    to ``_fetch_all()``, ensuring lazy querysets work correctly with RLS.
    """

    _rls_user: TenantUser | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rls_user = None

    def for_user(self, as_user: TenantUser) -> TenantQuerySet:
        """Scope this queryset to the given user's tenant.

        For admin users: returns all rows (RLS admin bypass at eval time).
        For tenant users: returns rows matching the user's tenant.

        The queryset remains lazy and chainable. GUC variables are set
        when the queryset is evaluated, not when ``for_user()`` is called.

        Args:
            as_user: User object satisfying the ``TenantUser`` protocol.
        """
        qs = self._clone()
        qs._rls_user = as_user  # noqa: SLF001
        if not as_user.is_tenant_admin:
            # Defense-in-depth: Django-level filter provides isolation
            # even if GUC is misconfigured. RLS provides isolation even
            # if the Django filter is bypassed (e.g., raw SQL).
            conf = rls_tenants_config
            qs = qs.filter(**{f"{conf.TENANT_FK_FIELD}_id": as_user.rls_tenant_id})
        return qs

    def _clone(self) -> TenantQuerySet:
        """Propagate ``_rls_user`` to cloned querysets."""
        clone: TenantQuerySet = super()._clone()  # type: ignore[misc]
        clone._rls_user = self._rls_user
        return clone

    def _fetch_all(self) -> None:
        """Set GUC variables just before query execution.

        Uses ``self.db`` so GUCs target the correct connection in
        multi-database setups (e.g., ``.using("replica")``).
        """
        if self._rls_user is not None:
            conf = rls_tenants_config
            db_alias = self.db
            guc_vars = _resolve_user_guc_vars(self._rls_user, conf)
            for guc_name, guc_value in guc_vars.items():
                if guc_value:
                    set_guc(guc_name, guc_value, is_local=conf.USE_LOCAL_SET, using=db_alias)
                else:
                    clear_guc(guc_name, is_local=conf.USE_LOCAL_SET, using=db_alias)
            try:
                super()._fetch_all()
            finally:
                if not conf.USE_LOCAL_SET:
                    clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
                    clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
        else:
            super()._fetch_all()


class RLSManager(models.Manager):  # type: ignore[type-arg]
    """Manager for RLS-protected models.

    Provides ``for_user()`` for scoped queries and
    ``prepare_tenant_in_model_data()`` for resolving tenant FKs.
    """

    def get_queryset(self) -> TenantQuerySet:
        """Return a ``TenantQuerySet`` instance."""
        return TenantQuerySet(self.model, using=self._db)

    def for_user(self, as_user: TenantUser) -> TenantQuerySet:
        """Return a queryset scoped to the given user's tenant."""
        return self.get_queryset().for_user(as_user=as_user)

    def prepare_tenant_in_model_data(
        self,
        model_data: dict[str, Any],
        as_user: TenantUser,  # noqa: ARG002  -- part of public API
    ) -> None:
        """Resolve a raw tenant ID for model creation.

        If ``model_data`` contains a raw tenant ID (int/str) under
        the configured FK field name, sets the FK column directly
        (``{field}_id``) to avoid a ``SELECT`` query. Allows passing
        ``tenant=42`` in creation data without N+1 overhead.

        Args:
            model_data: Dict of field names to values.
            as_user: User for context (unused here but part of API).
        """
        from django.apps import apps  # noqa: PLC0415  -- lazy import avoids circular

        conf = rls_tenants_config
        field_name = conf.TENANT_FK_FIELD
        tenant_model = apps.get_model(conf.TENANT_MODEL)

        tenant = model_data.get(field_name)
        if tenant is not None and not isinstance(tenant, tenant_model):
            # Set the FK column directly to avoid a model fetch query.
            # For bulk creates this eliminates N identical SELECTs.
            fk_column = f"{field_name}_id"
            model_data[fk_column] = tenant
            del model_data[field_name]
