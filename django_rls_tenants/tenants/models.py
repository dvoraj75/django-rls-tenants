"""``RLSProtectedModel`` -- abstract base for tenant-scoped models.

Uses the ``class_prepared`` signal to dynamically add a tenant
``ForeignKey`` to concrete subclasses, reading the target model
from ``RLS_TENANTS["TENANT_MODEL"]``.

Also auto-detects M2M fields on ``RLSProtectedModel`` subclasses
and registers ``RLSM2MConstraint`` on their auto-generated through
tables, and validates that a row's foreign keys to RLS-protected models
point at rows of its own tenant (``validate_cross_tenant_fks()``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models, router
from django.db.models.signals import class_prepared

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from typing import Any

from django_rls_tenants.rls.constraints import RLSConstraint, RLSM2MConstraint
from django_rls_tenants.tenants.context import admin_context
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
    if sender._meta.abstract or sender._meta.proxy:  # noqa: SLF001  -- standard Django _meta access
        return  # a proxy has its concrete model's field and cannot declare one

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
                logger.debug(
                    "register_m2m_rls: skipping %s.%s -- explicit through model",
                    model.__name__,
                    m2m_field.name,
                )
                continue

            # Skip if constraint already exists (e.g., from the other side)
            constraints = through_model._meta.constraints  # type: ignore[union-attr]  # noqa: SLF001
            if any(isinstance(c, RLSM2MConstraint) for c in constraints):
                logger.debug(
                    "register_m2m_rls: skipping %s.%s -- RLSM2MConstraint already exists",
                    model.__name__,
                    m2m_field.name,
                )
                continue

            from_model = model
            to_model_ref = m2m_field.related_model

            # Skip unresolved lazy references (shouldn't happen in ready())
            if isinstance(to_model_ref, str):
                logger.debug(
                    "register_m2m_rls: skipping %s.%s -- unresolved lazy reference %r",
                    model.__name__,
                    m2m_field.name,
                    to_model_ref,
                )
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
                logger.debug(
                    "register_m2m_rls: skipping %s.%s -- neither side is RLS-protected",
                    model.__name__,
                    m2m_field.name,
                )
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


def _tenant_field(model: type[models.Model]) -> models.ForeignKey[Any, Any] | None:
    """Return the foreign key ``model``'s ``RLSConstraint`` names, or ``None``.

    A proxy model's is its concrete model's (a proxy has no constraints of its
    own). ``None`` too when the model lacks that field (``W009`` reports it) or
    it is not a foreign key.
    """
    concrete = model._meta.concrete_model or model  # noqa: SLF001
    tenant_name = _get_tenant_fk_field(concrete)
    if tenant_name is None:
        return None
    try:
        field = concrete._meta.get_field(tenant_name)  # noqa: SLF001
    except FieldDoesNotExist:
        return None
    return field if isinstance(field, models.ForeignKey) else None


def _checked_foreign_keys(
    model: type[models.Model],
) -> tuple[models.ForeignKey[Any, Any], list[models.ForeignKey[Any, Any]]] | None:
    """Return ``model``'s tenant field and its foreign keys to RLS-protected models.

    The tenant field is the one the model's ``RLSConstraint`` names. The
    foreign keys (one-to-one fields included) are those whose target has an
    ``RLSConstraint``, except the tenant field and multi-table-inheritance
    parent links, which Django's own foreign key validation skips too.

    Returns:
        ``(tenant_field, foreign_keys)``, or ``None`` when ``model`` has no
        ``RLSConstraint`` or lacks the field it names (``W009``).
    """
    tenant_field = _tenant_field(model)
    if tenant_field is None:
        return None
    foreign_keys = [
        field
        for field in model._meta.concrete_fields  # noqa: SLF001
        if isinstance(field, models.ForeignKey)
        and field is not tenant_field
        and not field.remote_field.parent_link
        and isinstance(field.related_model, type)
        and _tenant_field(field.related_model) is not None
    ]
    return tenant_field, foreign_keys


def _invalid_target(field: models.ForeignKey[Any, Any], value: Any) -> ValidationError:
    """Return the error Django's ``ForeignKey.validate()`` raises for a missing target.

    The same message and code for a target of another tenant, so the error
    does not tell the writer that the row exists elsewhere.
    """
    return ValidationError(
        field.error_messages["invalid"],
        code="invalid",
        params={
            "model": field.related_model._meta.verbose_name,  # noqa: SLF001
            "pk": value,
            "field": field.remote_field.field_name,
            "value": value,
        },
    )


def _foreign_keys_of_other_tenants(
    tenant_id: Any,
    targets: Mapping[models.ForeignKey[Any, Any], Any],
    *,
    instance: models.Model | None = None,
) -> list[models.ForeignKey[Any, Any]]:
    """Return the foreign keys in ``targets`` whose target row is not of tenant ``tenant_id``.

    Each target's tenant is read with one ``values_list()`` query inside
    ``admin_context()``, so the answer does not depend on the active context.
    A target that does not exist, or whose tenant is ``NULL``, counts as
    another tenant's: a row of tenant ``tenant_id`` cannot see it either.

    Args:
        tenant_id: The tenant of the row that holds the foreign keys.
        targets: Each foreign key and the value it holds (its ``attname``
            value, already converted with ``to_python()``).
        instance: The row, passed to the router as Django's foreign key
            validation does.
    """
    by_alias: dict[str, list[tuple[models.ForeignKey[Any, Any], Any]]] = {}
    for field, value in targets.items():
        alias = router.db_for_read(field.related_model, instance=instance)
        by_alias.setdefault(alias, []).append((field, value))
    mismatched: list[models.ForeignKey[Any, Any]] = []
    for alias, pairs in by_alias.items():
        with admin_context(using=alias):
            for field, value in pairs:
                target: type[models.Model] = field.related_model
                target_tenant = _tenant_field(target)
                assert target_tenant is not None  # noqa: S101  -- _checked_foreign_keys() keeps only such targets
                tenants = list(
                    target._base_manager.using(alias)  # noqa: SLF001
                    .filter(**{field.target_field.name: value})
                    .values_list(target_tenant.attname, flat=True)[:1],
                )
                if tenants != [tenant_id]:
                    mismatched.append(field)
    return mismatched


def _cross_tenant_errors(
    instance: models.Model,
    exclude: Collection[str],
    *,
    failed: Collection[str] = (),
) -> dict[str, list[ValidationError]]:
    """Return the errors of ``instance``'s foreign keys that point at another tenant's row.

    Args:
        instance: The row to check.
        exclude: Foreign key names not to check. The tenant field is read
            even when it is excluded.
        failed: Fields that already failed validation: they are not checked,
            and nothing is when the tenant field is one of them.
    """
    checked = _checked_foreign_keys(type(instance))
    if checked is None:
        return {}
    tenant_field, foreign_keys = checked
    if tenant_field.name in failed:
        return {}
    try:
        tenant_id = tenant_field.to_python(getattr(instance, tenant_field.attname))
    except ValidationError:
        return {}  # the tenant field's own validation reports it
    if tenant_id is None:
        return {}  # no tenant yet (an admin form sets it after validation)
    errors: dict[str, list[ValidationError]] = {}
    targets: dict[models.ForeignKey[Any, Any], Any] = {}
    for field in foreign_keys:
        if field.name in exclude or field.name in failed:
            continue
        try:
            value = field.to_python(getattr(instance, field.attname))
        except ValidationError as error:
            errors[field.name] = error.error_list
            continue
        if value is not None:  # None: no target, or one that is not saved yet
            targets[field] = value
    if targets:
        for field in _foreign_keys_of_other_tenants(tenant_id, targets, instance=instance):
            errors[field.name] = [_invalid_target(field, targets[field])]
    return errors


def validate_cross_tenant_fks(
    instance: models.Model,
    *,
    exclude: Collection[str] | None = None,
) -> None:
    """Check that ``instance``'s foreign keys to RLS-protected models stay in its tenant.

    RLS cannot stop a row of tenant A from pointing at a row of tenant B:
    the policy checks the row's own tenant field, and PostgreSQL runs
    referential-integrity checks outside row security.
    ``RLSProtectedModel.clean_fields()`` calls this, so ``full_clean()``
    (``ModelForm``, the admin, an explicit call) rejects such a foreign key;
    ``save()``, ``bulk_create()``, ``update()`` and raw SQL do not. Call it
    yourself before a ``save()`` that no form validated.

    Each foreign key (one-to-one fields included) to a model with an
    ``RLSConstraint`` is compared with the instance's **own** tenant field,
    not with the active context, so an admin editing another tenant's row
    is not rejected. The target's tenant is read in ``admin_context()``
    with one query per foreign key, whatever the active context. Skipped:
    the tenant field itself, parent links, empty foreign keys and unsaved
    targets, and every foreign key while the instance has no tenant. A
    model without an ``RLSConstraint`` has nothing to check.

    The error is the one Django's foreign key validation gives a row that
    does not exist (the field's ``invalid`` message), so it does not reveal
    that the row exists in another tenant. A target that does not exist, or
    whose tenant field is ``NULL``, gets it too.

    Args:
        instance: The model instance to check.
        exclude: Names of foreign keys not to check. The tenant field is read
            even when it is excluded.

    Raises:
        ValidationError: Keyed by foreign key name, for each foreign key whose
            target is not a row of the instance's tenant.
    """
    errors = _cross_tenant_errors(instance, exclude or ())
    if errors:
        raise ValidationError(errors)


class_prepared.connect(_add_tenant_fk)


class RLSProtectedModel(models.Model):
    """Abstract base model for tenant-scoped models.

    Provides:

    - A ``tenant`` ForeignKey added dynamically via the ``class_prepared``
      signal (target read from ``RLS_TENANTS["TENANT_MODEL"]``).
    - ``RLSManager`` as the default manager (with ``for_user()``).
    - ``RLSConstraint`` in ``Meta.constraints`` (generates RLS policy).
    - ``clean_fields()`` that rejects a foreign key to another tenant's row
      (see :func:`validate_cross_tenant_fks`).

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

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        """Validate the fields, then the tenant of each foreign key's target.

        Runs :func:`validate_cross_tenant_fks` on the foreign keys that are
        not in ``exclude`` and have no error yet (a ``ModelForm`` excludes the
        fields it does not show).

        Raises:
            ValidationError: Django's field errors and those of foreign keys
                that point at another tenant's row.
        """
        errors: dict[str, list[ValidationError]] = {}
        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as error:
            errors = dict(error.error_dict)
        errors.update(_cross_tenant_errors(self, exclude or (), failed=errors))
        if errors:
            raise ValidationError(errors)
