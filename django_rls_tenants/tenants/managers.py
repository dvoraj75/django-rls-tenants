"""Tenant-aware manager and queryset.

``TenantQuerySet`` solves the lazy evaluation problem: it stores the
user reference from ``for_user()`` and sets GUC variables at query
evaluation time (in ``_fetch_all``), not at queryset creation time.

Auto-scope support: when a tenant context is active (via
``tenant_context()``, ``admin_context()``, or ``RLSTenantMiddleware``),
the queryset automatically adds ``WHERE tenant_id = X`` to every query.
For joins via ``select_related()``, tenant filters are also propagated
to joined RLS-protected tables, enabling PostgreSQL to use composite
indexes on both sides of the join.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from django.db import models

from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _resolve_user_guc_vars
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    reset_current_tenant_id,
    set_current_tenant_id,
)

if TYPE_CHECKING:
    from django_rls_tenants.tenants.types import TenantUser

logger = logging.getLogger("django_rls_tenants")


def _is_rls_protected(model: type[models.Model]) -> bool:
    """Check if a Django model has an ``RLSConstraint`` in its Meta.

    Uses lazy import and caching to avoid circular imports and repeated
    introspection.

    Args:
        model: A Django model class.

    Returns:
        ``True`` if the model has at least one ``RLSConstraint``.
    """
    return model in _rls_model_cache()


@lru_cache(maxsize=1)
def _rls_model_cache() -> frozenset[type[models.Model]]:
    """Build a cached set of all RLS-protected models.

    Called lazily after Django's app registry is ready. The result is
    cached for the lifetime of the process (model set doesn't change).
    """
    from django.apps import apps  # noqa: PLC0415  -- lazy import avoids circular

    from django_rls_tenants.rls.constraints import RLSConstraint  # noqa: PLC0415

    protected: set[type[models.Model]] = set()
    for model in apps.get_models():
        if any(isinstance(c, RLSConstraint) for c in model._meta.constraints):  # noqa: SLF001
            protected.add(model)
    return frozenset(protected)


class TenantQuerySet(models.QuerySet):  # type: ignore[type-arg]
    """QuerySet that sets RLS GUC variables at evaluation time.

    Stores the user reference from ``for_user()`` and defers GUC setup
    to ``_fetch_all()``, ensuring lazy querysets work correctly with RLS.

    When auto-scope is active (via ``tenant_context()`` or middleware),
    ``select_related()`` automatically adds ``WHERE related.tenant_id = X``
    for joined RLS-protected tables, enabling index usage on both sides
    of the join.
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

    def select_related(self, *fields: Any) -> TenantQuerySet:
        """Override to add tenant filters on joined RLS-protected tables.

        When a tenant context is active (or ``for_user()`` was called),
        adds ``WHERE related.tenant_id = X`` for each explicitly named
        relation that targets an RLS-protected model. This enables
        PostgreSQL to use composite indexes on joined tables instead of
        relying solely on per-row RLS ``current_setting()`` evaluation.

        Falls back to ``super().select_related()`` when no tenant scope
        is active or when called with no arguments (select-all mode).
        """
        qs: TenantQuerySet = super().select_related(*fields)
        tenant_id = self._get_active_tenant_id()
        if tenant_id is None or not fields:
            return qs
        conf = rls_tenants_config
        fk_field_id = f"{conf.TENANT_FK_FIELD}_id"
        for field_path in fields:
            related_model = _resolve_related_model(self.model, field_path)
            if related_model is not None and _is_rls_protected(related_model):
                qs = qs.filter(**{f"{field_path}__{fk_field_id}": tenant_id})
        return qs

    def _get_active_tenant_id(self) -> int | str | None:
        """Return the active tenant ID from ContextVar or ``_rls_user``.

        Checks the ContextVar first (set by ``tenant_context()`` / middleware),
        then falls back to ``_rls_user`` (set by ``for_user()``).
        """
        ctx_id = get_current_tenant_id()
        if ctx_id is not None:
            return ctx_id
        if self._rls_user is not None and not self._rls_user.is_tenant_admin:
            return self._rls_user.rls_tenant_id
        return None

    def _clone(self) -> TenantQuerySet:
        """Propagate ``_rls_user`` to cloned querysets."""
        clone: TenantQuerySet = super()._clone()  # type: ignore[misc]
        clone._rls_user = self._rls_user
        return clone

    def _fetch_all(self) -> None:
        """Set GUC variables just before query execution.

        Uses ``self.db`` so GUCs target the correct connection in
        multi-database setups (e.g., ``.using("replica")``).

        Also sets the ``ContextVar`` tenant state during evaluation so
        that any querysets created during fetch (e.g., prefetch queries,
        deferred loads) benefit from auto-scope.
        """
        if self._rls_user is not None:
            conf = rls_tenants_config
            db_alias = self.db
            guc_vars = _resolve_user_guc_vars(self._rls_user, conf)

            # Set ContextVar so prefetch/deferred querysets get auto-scope
            user = self._rls_user
            ctx_tenant_id = None if user.is_tenant_admin else user.rls_tenant_id
            token = set_current_tenant_id(ctx_tenant_id)

            for guc_name, guc_value in guc_vars.items():
                if guc_value:
                    set_guc(guc_name, guc_value, is_local=conf.USE_LOCAL_SET, using=db_alias)
                else:
                    clear_guc(guc_name, is_local=conf.USE_LOCAL_SET, using=db_alias)
            try:
                super()._fetch_all()
            finally:
                reset_current_tenant_id(token)
                if not conf.USE_LOCAL_SET:
                    clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
                    clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
        else:
            super()._fetch_all()


def _resolve_related_model(
    model: type[models.Model],
    field_path: str,
) -> type[models.Model] | None:
    """Walk a dotted field path to find the target model.

    For ``"category__parent"``, starts at ``model``, follows ``category``
    to its related model, then follows ``parent`` to the final model.

    Args:
        model: The starting Django model class.
        field_path: A ``__``-separated relation path (e.g., ``"category"``
            or ``"order__customer"``).

    Returns:
        The target model class, or ``None`` if the path is invalid.
    """
    current: type[models.Model] = model
    for part in field_path.split("__"):
        try:
            field = current._meta.get_field(part)  # noqa: SLF001
        except Exception:
            return None
        related = getattr(field, "related_model", None)
        if related is None or isinstance(related, str):
            # str means lazy reference like "self" -- skip
            return None
        current = related
    return current


class RLSManager(models.Manager):  # type: ignore[type-arg]
    """Manager for RLS-protected models.

    Provides ``for_user()`` for scoped queries and
    ``prepare_tenant_in_model_data()`` for resolving tenant FKs.
    """

    def get_queryset(self) -> TenantQuerySet:
        """Return a ``TenantQuerySet`` instance, auto-scoped if a tenant context is active.

        When ``tenant_context()``, ``admin_context()``, or ``RLSTenantMiddleware``
        has set a current tenant ID, the queryset is automatically filtered by
        ``WHERE tenant_id = X``. This enables PostgreSQL to use composite indexes
        instead of relying solely on RLS ``current_setting()`` calls.
        """
        qs = TenantQuerySet(self.model, using=self._db)
        tenant_id = get_current_tenant_id()
        if tenant_id is not None:
            conf = rls_tenants_config
            qs = qs.filter(**{f"{conf.TENANT_FK_FIELD}_id": tenant_id})
        return qs

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
