"""``RLSProtectedModel`` -- abstract base for tenant-scoped models.

Uses the ``class_prepared`` signal to dynamically add a tenant
``ForeignKey`` to concrete subclasses, reading the target model
from ``RLS_TENANTS["TENANT_MODEL"]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models.signals import class_prepared

if TYPE_CHECKING:
    from typing import Any

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.tenants.managers import RLSManager


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
