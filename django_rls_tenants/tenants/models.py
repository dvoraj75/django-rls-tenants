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
from contextlib import ExitStack
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import connections, models, router, transaction
from django.db.models.signals import class_prepared

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from typing import Any

from django_rls_tenants.rls.constraints import RLSConstraint, RLSM2MConstraint
from django_rls_tenants.tenants import audit
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
    """Return the tenant FK field name the policy of ``model``'s table filters on, or ``None``.

    The ``field`` of the first ``RLSConstraint`` in the model's
    ``Meta.constraints``; for a model without one, the field
    ``RLS_TENANTS["EXTERNAL_MODELS"]`` maps its label to (a model whose policy
    an ``AddRLSPolicy`` migration creates). A proxy model has neither: its
    table is its concrete model's.
    """
    meta = model._meta  # noqa: SLF001
    for c in meta.constraints:
        if isinstance(c, RLSConstraint):
            return c.field
    if meta.proxy:
        return None
    return _external_model_fields().get(meta.label_lower)


def _declares_rls_policy(model: type[models.Model]) -> bool:
    """Return whether ``model`` declares its table's RLS policy itself.

    An ``RLSProtectedModel`` subclass, or any model whose ``Meta.constraints``
    list an ``RLSConstraint``: the runtime protects both alike (auto-scope,
    strict mode, the cross-tenant foreign key check), so :func:`register_m2m_rls`
    covers their automatic M2M through tables and ``check_rls`` verifies both.
    A model ``RLS_TENANTS["EXTERNAL_MODELS"]`` lists does not count: its policy
    and the policies of its through tables come from the project's own
    migrations (#112). A proxy has no constraints of its own, so its concrete
    model is read.
    """
    concrete = model._meta.concrete_model or model  # noqa: SLF001
    if issubclass(concrete, RLSProtectedModel):
        return True
    return any(isinstance(c, RLSConstraint) for c in concrete._meta.constraints)  # noqa: SLF001


def _external_model_fields() -> Mapping[str, str]:
    """Return ``RLS_TENANTS["EXTERNAL_MODELS"]`` keyed by ``Options.label_lower``."""
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    return _normalized_external_models(tuple(rls_tenants_config.EXTERNAL_MODELS.items()))


@lru_cache(maxsize=1)
def _normalized_external_models(entries: tuple[tuple[str, str], ...]) -> Mapping[str, str]:
    """Key the ``EXTERNAL_MODELS`` entries by ``Options.label_lower``.

    The app label stays as given and the model name is lower-cased, as
    ``apps.get_model()`` resolves them (and ``STRICT_MODE_EXCLUDE`` labels are
    read); a label that matches no model is reported by check ``W016``.
    """
    normalized: dict[str, str] = {}
    for label, field in entries:
        app_label, _, model_name = label.partition(".")
        normalized[f"{app_label}.{model_name.lower()}"] = field
    return MappingProxyType(normalized)


def register_m2m_rls() -> None:
    """Discover M2M fields on all RLS-protected models and add constraints.

    Iterates over all registered models, finds auto-generated M2M through
    tables on the models that declare their own RLS policy
    (:func:`_declares_rls_policy`: ``RLSProtectedModel`` subclasses and models
    with an ``RLSConstraint``), and adds an ``RLSM2MConstraint`` to each one.

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
        if not _declares_rls_policy(model) or model._meta.abstract:  # noqa: SLF001
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
            if _declares_rls_policy(to_model):
                # A proxy has no constraints of its own: read its concrete model's.
                to_tenant_fk = _get_tenant_fk_field(to_model._meta.concrete_model or to_model)  # noqa: SLF001
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
    """Return the foreign key ``model``'s policy filters on, or ``None``.

    The one its ``RLSConstraint`` or its ``EXTERNAL_MODELS`` entry names (see
    :func:`_get_tenant_fk_field`). A proxy model's is its concrete model's (a
    proxy has no constraints of its own). ``None`` too when the model lacks
    that field (``W009`` / ``W016`` report it) or it is not a foreign key.
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

    The tenant field is the one the model's ``RLSConstraint`` (or its
    ``EXTERNAL_MODELS`` entry) names. The foreign keys (one-to-one fields
    included) are those whose target is RLS-protected that way, except the
    tenant field and multi-table-inheritance parent links, which Django's own
    foreign key validation skips too.

    Returns:
        ``(tenant_field, foreign_keys)``, or ``None`` when ``model`` has no
        ``RLSConstraint`` or ``EXTERNAL_MODELS`` entry, or lacks the field it
        names (``W009`` / ``W016``).
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


def _needs_own_transaction(alias: str) -> bool:
    """Whether ``admin_context(using=alias)`` needs a transaction opened for it here.

    With ``USE_LOCAL_SET=True`` a context block on an alias outside
    ``RLS_TENANTS["DATABASES"]`` (a read replica a router returns, say) writes
    ``SET LOCAL`` itself, which fails outside ``transaction.atomic()``; on a
    configured alias the GUC affinity guard applies the values instead.
    """
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    conf = rls_tenants_config
    return (
        conf.USE_LOCAL_SET
        and alias not in conf.DATABASES
        and not connections[alias].in_atomic_block
    )


def _foreign_keys_of_other_tenants(
    tenant_id: Any,
    targets: Mapping[models.ForeignKey[Any, Any], Any],
    *,
    model: type[models.Model],
    instance: models.Model | None = None,
    source: audit.Source = "validation",
    user_id: Any = None,
    rejected: Mapping[models.ForeignKey[Any, Any], Any] | None = None,
) -> list[models.ForeignKey[Any, Any]]:
    """Return the foreign keys in ``targets`` whose target row is not of tenant ``tenant_id``.

    Each target's tenant is read with one ``values_list()`` query inside
    ``admin_context()`` (a lookup the audit log leaves out), so the answer
    does not depend on the active context. A target that does not exist, or
    whose tenant is ``NULL``, counts as another tenant's: a row of tenant
    ``tenant_id`` cannot see it either.

    The foreign keys whose target exists but is not of tenant ``tenant_id``
    are logged as one ``fk.cross_tenant_rejected`` audit record. That
    includes those in ``rejected``: foreign keys the caller's validation has
    already rejected as not found, which is what Django's and DRF's own
    validation report for a row that RLS or the auto-scope filter hides. They
    are looked up for the audit record only, only when the audit logger logs
    ``WARNING``, and in a savepoint whose failure loses the record but never
    changes the validation (:func:`_audited_target_tenants`).

    Args:
        tenant_id: The tenant of the row that holds the foreign keys.
        targets: Each foreign key and the value it holds (its ``attname``
            value, already converted with ``to_python()``).
        model: The model of the row, for the audit record.
        instance: The row, passed to the router as Django's foreign key
            validation does.
        source: Who validates, for the audit record.
        user_id: The primary key of the user who sent the data, if known.
        rejected: Foreign keys already rejected as not found, and their values.
    """
    found = _target_tenants(targets, instance=instance)
    if rejected and audit.audit_logger.isEnabledFor(logging.WARNING):
        found.update(_audited_target_tenants(rejected, instance=instance))
    # Exists, in another tenant or in none.
    cross_tenant = sorted(
        (field for field, tenants in found.items() if tenants and tenants != [tenant_id]),
        key=lambda field: field.creation_counter,
    )
    if cross_tenant:
        audit.emit(
            audit.FK_CROSS_TENANT_REJECTED,
            source=source,
            level=logging.WARNING,
            tenant_id=tenant_id,
            user_id=user_id,
            model=model._meta.label,  # noqa: SLF001
            fields=[field.name for field in cross_tenant],
        )
    return [field for field in targets if found[field] != [tenant_id]]


def _target_tenants(
    targets: Mapping[models.ForeignKey[Any, Any], Any],
    *,
    instance: models.Model | None,
    savepoint: bool = False,
) -> dict[models.ForeignKey[Any, Any], list[Any]]:
    """Return each foreign key's target tenant: ``[tenant]``, or ``[]`` when there is no target.

    One ``values_list()`` query per foreign key, inside ``admin_context()`` on
    the router's read alias (see :func:`_foreign_keys_of_other_tenants`).

    Args:
        targets: Each foreign key and the value to look up.
        instance: The row, passed to the router.
        savepoint: Run each alias's lookups in ``transaction.atomic()`` (a
            savepoint inside an open transaction), so a failure leaves the
            caller's transaction usable.
    """
    by_alias: dict[str, list[tuple[models.ForeignKey[Any, Any], Any]]] = {}
    for field, value in targets.items():
        alias = router.db_for_read(field.related_model, instance=instance)
        by_alias.setdefault(alias, []).append((field, value))
    found: dict[models.ForeignKey[Any, Any], list[Any]] = {}
    for alias, pairs in by_alias.items():
        with ExitStack() as stack:
            if savepoint or _needs_own_transaction(alias):
                stack.enter_context(transaction.atomic(using=alias))
            stack.enter_context(audit.silenced(admin_context(using=alias)))
            for field, value in pairs:
                target: type[models.Model] = field.related_model
                target_tenant = _tenant_field(target)
                assert target_tenant is not None  # noqa: S101  -- _checked_foreign_keys() keeps only such targets
                found[field] = list(
                    target._base_manager.using(alias)  # noqa: SLF001
                    .filter(**{field.target_field.name: value})
                    .values_list(target_tenant.attname, flat=True)[:1],
                )
    return found


def _audited_target_tenants(
    rejected: Mapping[models.ForeignKey[Any, Any], Any],
    *,
    instance: models.Model | None,
) -> dict[models.ForeignKey[Any, Any], list[Any]]:
    """Return :func:`_target_tenants` of ``rejected``, or ``{}`` if the lookup fails.

    The lookup only feeds the audit record: validation has already rejected
    these values, so a failure (a replica down, a statement timeout, a
    transaction that has already failed) loses the record, never the error the
    caller reports. It runs in a savepoint, which keeps an open transaction
    usable when it fails.
    """
    try:
        return _target_tenants(rejected, instance=instance, savepoint=True)
    except Exception:
        logger.debug(
            "Audit: could not look up the tenants of rejected foreign key targets.",
            exc_info=True,
        )
        return {}


def _audit_rejected_values(
    model: type[models.Model],
    tenant_id: Any,
    values: Mapping[models.ForeignKey[Any, Any], Any],
    *,
    instance: models.Model | None,
    source: audit.Source,
    user_id: Any,
) -> None:
    """Log the foreign keys an integration's validation rejected as not found, if cross-tenant.

    For a form's ``ModelChoiceField`` (``invalid_choice``) or a DRF
    ``PrimaryKeyRelatedField`` (``does_not_exist``): in a tenant context their
    queryset hides another tenant's rows, so they reject such a target before
    :func:`validate_cross_tenant_fks` sees it. Audit-only (see
    :func:`_foreign_keys_of_other_tenants`).

    Args:
        model: The model of the row.
        tenant_id: The tenant of the row (the active one), converted here.
        values: Each foreign key and the value sent for it, converted here
            with the target field's ``to_python()``; one that does not convert
            is not a target and is skipped.
        instance: The row, passed to the router.
        source: Who validates, for the audit record.
        user_id: The primary key of the user who sent the data, if known.
    """
    checked = _checked_foreign_keys(model)
    if checked is None or not audit.audit_logger.isEnabledFor(logging.WARNING):
        return
    try:
        tenant_id = checked[0].to_python(tenant_id)
    except ValidationError:
        return
    rejected: dict[models.ForeignKey[Any, Any], Any] = {}
    for field, value in values.items():
        try:
            converted = field.target_field.to_python(value)
        except (ValidationError, TypeError, ValueError):
            continue  # not a value of the target's type
        if converted not in (None, ""):
            rejected[field] = converted
    if rejected:
        _foreign_keys_of_other_tenants(
            tenant_id,
            {},
            model=model,
            instance=instance,
            source=source,
            user_id=user_id,
            rejected=rejected,
        )


def _not_found_value(
    instance: models.Model,
    field: models.ForeignKey[Any, Any],
    errors: list[ValidationError],
) -> Any:
    """Return ``field``'s value if ``errors`` rejected its target as not found, else ``None``.

    Django's foreign key validation reports a target that the active context
    cannot see (RLS hides another tenant's rows) with the ``invalid`` code.
    """
    if not any(error.code == "invalid" for error in errors):
        return None
    try:
        return field.to_python(getattr(instance, field.attname))
    except ValidationError:
        return None  # not a value of the target's type


def _cross_tenant_errors(
    instance: models.Model,
    exclude: Collection[str],
    *,
    failed: Mapping[str, list[ValidationError]] | None = None,
) -> dict[str, list[ValidationError]]:
    """Return the errors of ``instance``'s foreign keys that point at another tenant's row.

    Args:
        instance: The row to check.
        exclude: Foreign key names not to check. The tenant field is read
            even when it is excluded.
        failed: Fields that already failed validation, and their errors: they
            are not checked, and nothing is when the tenant field is one of
            them. A foreign key that failed as not found (Django's ``invalid``
            error, which is what it reports for a row RLS hides) is looked up
            for the ``fk.cross_tenant_rejected`` audit record.
    """
    failed = failed or {}
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
    # Rejected as not found already: looked up for the audit record only.
    rejected: dict[models.ForeignKey[Any, Any], Any] = {}
    for field in foreign_keys:
        if field.name in exclude:
            continue
        if field.name in failed:
            value = _not_found_value(instance, field, failed[field.name])
            if value is not None:
                rejected[field] = value
            continue
        try:
            value = field.to_python(getattr(instance, field.attname))
        except ValidationError as error:
            errors[field.name] = error.error_list
            continue
        if value is not None:  # None: no target, or one that is not saved yet
            targets[field] = value
    if targets or rejected:
        for field in _foreign_keys_of_other_tenants(
            tenant_id,
            targets,
            model=type(instance),
            instance=instance,
            rejected=rejected,
        ):
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
    ``RLSConstraint``, or listed in ``RLS_TENANTS["EXTERNAL_MODELS"]``, is
    compared with the instance's **own** tenant field, not with the active
    context, so an admin editing another tenant's row
    is not rejected. The target's tenant is read in ``admin_context()``
    with one query per foreign key, whatever the active context. Skipped:
    the tenant field itself, parent links, empty foreign keys and unsaved
    targets, and every foreign key while the instance has no tenant. A
    model without an ``RLSConstraint`` (or ``EXTERNAL_MODELS`` entry) has
    nothing to check.

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
