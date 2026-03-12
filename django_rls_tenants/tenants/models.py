"""``RLSProtectedModel`` -- abstract base for tenant-scoped models.

Uses ``__init_subclass__`` to dynamically add a tenant ``ForeignKey``
to concrete subclasses, reading the target model from
``RLS_TENANTS["TENANT_MODEL"]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from typing import Any

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.tenants.managers import RLSManager


class RLSProtectedModel(models.Model):
    """Abstract base model for tenant-scoped models.

    Provides:

    - A ``tenant`` ForeignKey added dynamically via ``__init_subclass__``
      (target read from ``RLS_TENANTS["TENANT_MODEL"]``).
    - ``RLSManager`` as the default manager (with ``for_user()``).
    - ``RLSConstraint`` in ``Meta.constraints`` (generates RLS policy).

    Usage::

        class Order(RLSProtectedModel):
            product = models.CharField(max_length=255)
            amount = models.DecimalField(...)

            class Meta(RLSProtectedModel.Meta):
                db_table = "order"

    To customize the tenant FK (e.g., nullable for admin users),
    declare the field directly on your model -- ``__init_subclass__``
    will not add a duplicate::

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

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls._meta.abstract:
            return

        # Skip if the subclass already defines its own tenant field
        local_field_names = [f.name for f in cls._meta.local_fields]
        if "tenant" not in local_field_names:
            from django_rls_tenants.tenants.conf import (  # noqa: PLC0415
                rls_tenants_config,
            )

            field: models.ForeignKey[Any, Any] = models.ForeignKey(
                to=rls_tenants_config.TENANT_MODEL,
                on_delete=models.CASCADE,
                blank=False,
                null=False,
            )
            field.contribute_to_class(cls, rls_tenants_config.TENANT_FK_FIELD)
