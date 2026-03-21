"""``RLSProtectedModel`` -- abstract base for tenant-scoped models.

Uses the ``class_prepared`` signal to dynamically add a tenant
``ForeignKey`` to concrete subclasses, reading the target model
from ``RLS_TENANTS["TENANT_MODEL"]``.

Also auto-detects M2M fields on ``RLSProtectedModel`` subclasses
and registers ``RLSM2MConstraint`` on their auto-generated through
tables.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import models
from django.db.models.signals import class_prepared

if TYPE_CHECKING:
    from typing import Any

from django_rls_tenants.rls.constraints import RLSConstraint, RLSM2MConstraint
from django_rls_tenants.tenants.managers import RLSManager

logger = logging.getLogger("django_rls_tenants")


def _add_tenant_fk(sender: type, **kwargs: Any) -> None:  # noqa: ARG001  -- signal handler signature
    """Add a tenant FK to concrete ``RLSProtectedModel`` subclasses.

    Connected to Django's ``class_prepared`` signal so it fires after
    ``ModelBase.__new__`` has fully initialised ``_meta``.  This avoids
    the timing issue with ``__init_subclass__`` where ``_meta`` still
    belongs to the parent and reports ``abstract=True``.
    """
    if not issubclass(sender, RLSProtectedModel) or sender is RLSProtectedModel:
        return
    if sender._meta.abstract:  # noqa: SLF001  -- standard Django _meta access
        return

    # Skip if the subclass already defines its own tenant field
    from django_rls_tenants.tenants.conf import (  # noqa: PLC0415
        rls_tenants_config,
    )

    local_field_names = [f.name for f in sender._meta.local_fields]  # noqa: SLF001
    field_name = rls_tenants_config.TENANT_FK_FIELD
    if field_name not in local_field_names:
        field: models.ForeignKey[Any, Any] = models.ForeignKey(
            to=rls_tenants_config.TENANT_MODEL,
            on_delete=models.CASCADE,
            blank=False,
            null=False,
        )
        field.contribute_to_class(sender, field_name)


def _get_tenant_fk_field(model: type[models.Model]) -> str | None:
    """Return the tenant FK field name if ``model`` has an ``RLSConstraint``, else ``None``.

    Checks for an ``RLSConstraint`` in the model's ``Meta.constraints``
    and returns its ``field`` attribute.
    """
    for c in model._meta.constraints:  # noqa: SLF001
        if isinstance(c, RLSConstraint):
            return c.field
    return None


def register_m2m_rls() -> None:
    """Discover M2M fields on all RLS-protected models and add constraints.

    Iterates over all registered models, finds auto-generated M2M through
    tables on ``RLSProtectedModel`` subclasses, and adds an
    ``RLSM2MConstraint`` to each one.

    Called from ``DjangoRlsTenantsConfig.ready()`` when the app registry
    is fully populated. Can also be called manually for testing.

    Skips:
    - Explicit through models (user handles those via standard ``RLSProtectedModel``)
    - Through models that already have an ``RLSM2MConstraint``
    - M2M fields where neither side is RLS-protected
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model is RLSProtectedModel:
            continue
        if model._meta.abstract:  # noqa: SLF001
            continue

        for m2m_field in model._meta.local_many_to_many:  # noqa: SLF001
            through_model = m2m_field.remote_field.through

            # Skip explicit through models -- user manages RLS themselves
            if not through_model._meta.auto_created:  # type: ignore[union-attr]  # noqa: SLF001
                continue

            # Skip if constraint already exists (e.g., from the other side)
            constraints = through_model._meta.constraints  # type: ignore[union-attr]  # noqa: SLF001
            if any(isinstance(c, RLSM2MConstraint) for c in constraints):
                continue

            from_model = model
            to_model_ref = m2m_field.related_model

            # Skip unresolved lazy references (shouldn't happen in ready())
            if isinstance(to_model_ref, str):
                continue

            to_model: type[models.Model] = to_model_ref

            # Resolve FK column names from the M2M internals
            m2m_col = m2m_field.m2m_column_name()
            m2m_reverse_col = m2m_field.m2m_reverse_name()

            # Determine which sides are RLS-protected
            from_tenant_fk = _get_tenant_fk_field(from_model)
            if from_tenant_fk is None:
                # from_model is RLSProtectedModel but without explicit RLSConstraint;
                # use default tenant FK field
                from_tenant_fk = rls_tenants_config.TENANT_FK_FIELD

            to_tenant_fk: str | None = None
            if issubclass(to_model, RLSProtectedModel):
                to_tenant_fk = _get_tenant_fk_field(to_model)
                if to_tenant_fk is None:
                    to_tenant_fk = rls_tenants_config.TENANT_FK_FIELD

            # Need at least one protected side
            if from_tenant_fk is None and to_tenant_fk is None:
                continue

            from_path = f"{from_model._meta.app_label}.{from_model.__name__}"  # noqa: SLF001
            to_path = f"{to_model._meta.app_label}.{to_model.__name__}"  # noqa: SLF001
            table: str = through_model._meta.db_table  # type: ignore[union-attr]  # noqa: SLF001

            constraint = RLSM2MConstraint(
                name=f"{table}_m2m_rls_constraint",
                from_model=from_path,
                to_model=to_path,
                from_fk=m2m_col,
                to_fk=m2m_reverse_col,
                from_tenant_fk=from_tenant_fk,
                to_tenant_fk=to_tenant_fk,
            )
            constraints.append(constraint)  # type: ignore[union-attr]
            logger.debug(
                "Added RLSM2MConstraint to %s (from=%s, to=%s)",
                table,
                from_path,
                to_path,
            )


class_prepared.connect(_add_tenant_fk)


class RLSProtectedModel(models.Model):
    """Abstract base model for tenant-scoped models.

    Provides:

    - A ``tenant`` ForeignKey added dynamically via the ``class_prepared``
      signal (target read from ``RLS_TENANTS["TENANT_MODEL"]``).
    - ``RLSManager`` as the default manager (with ``for_user()``).
    - ``RLSConstraint`` in ``Meta.constraints`` (generates RLS policy).

    Usage::

        class Order(RLSProtectedModel):
            product = models.CharField(max_length=255)
            amount = models.DecimalField(...)

            class Meta(RLSProtectedModel.Meta):
                db_table = "order"

    To customize the tenant FK (e.g., nullable for admin users),
    declare the field directly on your model -- the ``class_prepared``
    handler will not add a duplicate::

        class User(AbstractUser, RLSProtectedModel):
            tenant = models.ForeignKey(
                Tenant, on_delete=models.CASCADE,
                null=True, blank=True,
            )
    """

    objects = RLSManager()

    class Meta:
        abstract = True
        constraints = [  # noqa: RUF012  -- Django Meta convention
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
            ),
        ]
