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

.. note:: **``for_user()`` GUC limitation**

    The ``for_user()`` mechanism only sets GUC variables during
    ``_fetch_all()`` (iteration). Methods that bypass ``_fetch_all``
    (``count()``, ``exists()``, ``aggregate()``, ``update()``,
    ``delete()``, ``iterator()``) do **not** trigger GUC setting.
    For non-middleware entry points (Celery tasks, management commands),
    prefer ``tenant_context()`` or ``admin_context()`` which set GUCs
    at the connection level for the entire block.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Q

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _resolve_user_guc_vars
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    get_rls_context_active,
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

    Raises:
        RuntimeError: If called before ``django.apps`` is fully ready,
            to prevent caching an incomplete model set.
    """
    from django.apps import apps  # noqa: PLC0415  -- lazy import avoids circular

    from django_rls_tenants.rls.constraints import RLSConstraint  # noqa: PLC0415

    if not apps.ready:
        msg = (
            "_rls_model_cache() called before Django apps are ready. "
            "This would cache an incomplete model set. Ensure all apps "
            "are loaded before accessing RLS-protected querysets."
        )
        raise RuntimeError(msg)

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

    .. warning:: **Limitation of ``for_user()`` GUC management**

        GUC variables are only set during ``_fetch_all()`` (iteration).
        QuerySet methods that bypass ``_fetch_all()`` — such as
        ``count()``, ``exists()``, ``aggregate()``, ``update()``,
        ``delete()``, and ``iterator()`` — will **not** have GUC
        variables set by ``for_user()``.

        For non-admin users this is safe because ``for_user()`` also
        adds a Django ORM ``WHERE tenant_id = X`` filter. For **admin
        users** (``is_tenant_admin=True``), no ORM filter is applied,
        so these methods run against whatever GUC state the connection
        already has.

        For non-middleware contexts (Celery tasks, management commands),
        use ``tenant_context()`` or ``admin_context()`` instead, which
        set GUCs at the connection level for the entire block.
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

        .. warning::

            GUC variables are only set during iteration (``_fetch_all``).
            Methods like ``count()``, ``exists()``, ``aggregate()``,
            ``update()``, ``delete()``, and ``iterator()`` bypass
            ``_fetch_all`` and will **not** have GUC variables set.
            For admin users this means those methods run without GUC
            protection. Use ``tenant_context()`` or ``admin_context()``
            for full coverage in non-middleware contexts.

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

        Handles ``select_related(False)`` (Django 5.x) and
        ``select_related(None)`` (Django 6.0+) for clearing without
        adding tenant filters.
        """
        # Django 5.x uses False, Django 6.0+ uses None to clear
        # select_related. Normalize to None before calling super() to
        # avoid AttributeError: 'bool' object has no attribute 'split'.
        if fields in ((False,), (None,)):
            return super().select_related(None)
        if not fields:
            return super().select_related()
        tenant_id = self._get_active_tenant_id()
        qs: TenantQuerySet = super().select_related(*fields)
        if tenant_id is None:
            return qs
        conf = rls_tenants_config
        fk_field_id = f"{conf.TENANT_FK_FIELD}_id"
        for field_path in fields:
            related_model = _resolve_related_model(self.model, field_path)
            if related_model is not None and _is_rls_protected(related_model):
                tenant_filter = Q(**{f"{field_path}__{fk_field_id}": tenant_id})
                # For nullable FKs, preserve LEFT OUTER JOIN semantics:
                # include rows where the FK is NULL (no related object).
                # Without this, .filter() forces an INNER JOIN which
                # silently drops rows with NULL FKs.
                try:
                    fk_field = self.model._meta.get_field(field_path)  # noqa: SLF001
                    if getattr(fk_field, "null", False):
                        tenant_filter = tenant_filter | Q(**{f"{field_path}__isnull": True})
                except FieldDoesNotExist:
                    pass
                qs = qs.filter(tenant_filter)
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

    # ---- Strict mode guard ----

    def _check_strict_mode(self) -> None:
        """Raise if strict mode is on and no RLS context is active.

        Only applies to RLS-protected models (those with an
        ``RLSConstraint``). Non-protected models that happen to use
        ``TenantQuerySet`` (e.g., via inheritance) are not guarded.

        The check reads two ``ContextVar`` values -- negligible cost.

        Raises:
            NoTenantContextError: If ``STRICT_MODE=True`` and neither a
                context manager/middleware nor ``for_user()`` has
                established an RLS context.
        """
        if not rls_tenants_config.STRICT_MODE:
            return
        if not _is_rls_protected(self.model):
            return  # not an RLS-protected model; skip guard
        if get_rls_context_active():
            return  # tenant_context / admin_context / middleware active
        if self._rls_user is not None:
            return  # for_user() was called
        model_name = self.model.__name__
        msg = (
            f"RLS strict mode: query on {model_name} attempted without "
            f"tenant context. Use tenant_context(), admin_context(), "
            f"for_user(), or RLSTenantMiddleware before querying "
            f"RLS-protected models. Set STRICT_MODE=False in RLS_TENANTS "
            f"to disable this check."
        )
        raise NoTenantContextError(msg)

    # ---- Guarded evaluation methods ----
    #
    # Methods like first()/last()/get() may also trigger _fetch_all()
    # on a cloned queryset internally, resulting in a double check.
    # This is intentional defense-in-depth: the direct check here
    # catches the call at the public API boundary, while the
    # _fetch_all() check catches any code path that reaches
    # evaluation without going through these overrides.

    def count(self) -> int:
        """Guard ``count()`` with strict mode check."""
        self._check_strict_mode()
        return super().count()

    def exists(self) -> bool:
        """Guard ``exists()`` with strict mode check."""
        self._check_strict_mode()
        return super().exists()

    def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Guard ``aggregate()`` with strict mode check."""
        self._check_strict_mode()
        return super().aggregate(*args, **kwargs)

    def update(self, **kwargs: Any) -> int:
        """Guard ``update()`` with strict mode check."""
        self._check_strict_mode()
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        """Guard ``delete()`` with strict mode check."""
        self._check_strict_mode()
        return super().delete()

    def iterator(self, chunk_size: int | None = None) -> Any:
        """Guard ``iterator()`` with strict mode check."""
        self._check_strict_mode()
        return super().iterator(chunk_size=chunk_size)

    def bulk_create(
        self,
        objs: Any,
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Any = None,
        unique_fields: Any = None,
    ) -> list[Any]:
        """Guard ``bulk_create()`` with strict mode check."""
        self._check_strict_mode()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(
        self,
        objs: Any,
        fields: Any,
        batch_size: int | None = None,
    ) -> int:
        """Guard ``bulk_update()`` with strict mode check."""
        self._check_strict_mode()
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Guard ``get()`` with strict mode check."""
        self._check_strict_mode()
        return super().get(*args, **kwargs)

    def first(self) -> Any:
        """Guard ``first()`` with strict mode check."""
        self._check_strict_mode()
        return super().first()

    def last(self) -> Any:
        """Guard ``last()`` with strict mode check."""
        self._check_strict_mode()
        return super().last()

    def _fetch_all(self) -> None:
        """Set GUC variables just before query execution.

        Uses ``self.db`` so GUCs target the correct connection in
        multi-database setups (e.g., ``.using("replica")``).

        Also sets the ``ContextVar`` tenant state during evaluation so
        that any querysets created during fetch (e.g., prefetch queries,
        deferred loads) benefit from auto-scope.
        """
        self._check_strict_mode()
        if self._rls_user is not None:
            conf = rls_tenants_config
            db_alias = self.db
            guc_vars = _resolve_user_guc_vars(self._rls_user, conf)

            # Set ContextVar so prefetch/deferred querysets get auto-scope
            user = self._rls_user
            ctx_tenant_id = None if user.is_tenant_admin else user.rls_tenant_id
            token = set_current_tenant_id(ctx_tenant_id)
            try:
                for guc_name, guc_value in guc_vars.items():
                    if guc_value:
                        set_guc(guc_name, guc_value, is_local=conf.USE_LOCAL_SET, using=db_alias)
                    else:
                        clear_guc(guc_name, is_local=conf.USE_LOCAL_SET, using=db_alias)
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
        except FieldDoesNotExist:
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
