"""Django REST Framework integration: the RLS context for API views.

DRF authenticates in ``APIView.initial()``, inside the view, after every Django
middleware has run. A client that authenticates with a token, a JWT or any other
DRF authentication class therefore reaches
:class:`~django_rls_tenants.tenants.middleware.RLSTenantMiddleware` as an
anonymous user, and the middleware sets no context: the view reads zero rows
(fail-closed) or, with ``STRICT_MODE``, raises. This module opens the context
where DRF knows the user:

- :class:`RLSTenantAPIViewMixin` -- a mixin for any ``APIView`` (generic views
  and viewsets included) that enters ``tenant_context()`` / ``admin_context()``
  for ``request.user`` right after DRF authenticates it, keeps it for the
  permission checks, the handler and the rendering of the response, and exits
  it before ``dispatch()`` returns.
- :class:`RLSTenantPermission` -- a permission class that denies a request
  served outside the context its user implies (a view without the mixin, say)
  or the tenant context the mixin entered for it, and an object of another
  tenant.
- :class:`RLSTenantSerializerMixin` / :class:`RLSTenantModelSerializer` -- a
  ``ModelSerializer`` that hides the tenant foreign key and fills it from the
  active context, and rejects a foreign key to another tenant's row;
  :class:`CurrentTenantDefault` is its field default.

There is deliberately no authentication class that sets the context: DRF never
calls an authenticator back when the request ends, so it could not exit the
context it entered.

This is an **optional** integration. It imports :mod:`rest_framework`, which is
not a dependency of the core library; install it with ``pip install
django-rls-tenants[drf]``. Nothing in ``tenants/`` or ``rls/`` imports this
module, and it is intentionally **not** re-exported from the top-level package.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

# Not rest_framework.views: DRF imports DEFAULT_PERMISSION_CLASSES while it builds
# APIView, so with RLSTenantPermission listed there, importing APIView here would
# be circular. It is imported where it is used.
try:
    from rest_framework import serializers
    from rest_framework.exceptions import ErrorDetail, PermissionDenied
    from rest_framework.fields import empty
    from rest_framework.permissions import BasePermission
    from rest_framework.settings import api_settings
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    msg = (
        "django_rls_tenants.contrib.drf requires Django REST Framework. "
        "Install it with: pip install django-rls-tenants[drf]"
    )
    raise ImportError(msg) from exc

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connections
from django.db.models import Model
from django.template.response import SimpleTemplateResponse
from django.utils.encoding import smart_str

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.errors import HINT_NO_CONTEXT, HINT_USER_NO_TENANT
from django_rls_tenants.tenants.state import (
    ADMIN_GUCS,
    ContextGucs,
    _current_live,
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from django.db.models import ForeignKey
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase
    from rest_framework.request import Request
    from rest_framework.views import APIView

    from django_rls_tenants.tenants.state import _Live

    # The mixins are combined with these classes; mypy checks them as subclasses.
    _APIViewBase = APIView
    _ModelSerializerBase = serializers.ModelSerializer[Any]
else:
    _APIViewBase = object
    _ModelSerializerBase = object

__all__ = [
    "CurrentTenantDefault",
    "RLSTenantAPIViewMixin",
    "RLSTenantModelSerializer",
    "RLSTenantPermission",
    "RLSTenantSerializerMixin",
]

logger = logging.getLogger("django_rls_tenants")

# The 403 an API client sees for a non-admin user without a tenant. The hint,
# which names the TenantUser attributes, goes to the log instead.
_NO_TENANT_DETAIL = "Your account is not assigned to a tenant."


def _context_gucs_for(user: Any) -> ContextGucs | None:
    """Return the GUC values of the context ``user`` implies; ``None`` for a tenantless non-admin.

    Args:
        user: An authenticated user satisfying the ``TenantUser`` protocol.
    """
    if user.is_tenant_admin:
        return ADMIN_GUCS
    tenant_id: int | str | None = user.rls_tenant_id
    return None if tenant_id is None else ContextGucs.for_tenant(tenant_id)


def _rollback_pending() -> bool:
    """Return whether an open ``atomic()`` block on this thread is marked for rollback.

    DRF's exception handler marks the ``ATOMIC_REQUESTS`` transaction so on a
    handled error; Django refuses every query in it until the block exits.
    """
    return any(
        conn.in_atomic_block and conn.needs_rollback
        for conn in connections.all(initialized_only=True)
    )


def _is_authenticated(user: Any) -> bool:
    """Return whether DRF authenticated ``user`` (``request.user`` may be ``None``)."""
    return user is not None and bool(user.is_authenticated)


class RLSTenantAPIViewMixin(_APIViewBase):
    """Run a DRF view inside the RLS context of ``request.user``.

    List it before the DRF class::

        from rest_framework import viewsets
        from django_rls_tenants.contrib.drf import RLSTenantAPIViewMixin

        class OrderViewSet(RLSTenantAPIViewMixin, viewsets.ModelViewSet):
            serializer_class = OrderSerializer

            def get_queryset(self):
                return Order.objects.all()

    Right after DRF authenticates the request (``perform_authentication()``,
    before the permission and throttle checks), the mixin enters
    ``admin_context()`` for a user with ``is_tenant_admin`` and
    ``tenant_context(user.rls_tenant_id)`` for any other user (the
    :class:`~django_rls_tenants.tenants.types.TenantUser` protocol). The
    context stays active for the permission checks, the handler, DRF's
    exception handler and the rendering of the response, and is exited before
    ``dispatch()`` returns, also when the view raises. A non-admin user
    without a tenant gets a 403.

    For a request DRF did not authenticate the mixin enters nothing: the view
    runs in whatever context encloses it, which is none (fail-closed) unless
    ``RLSTenantMiddleware`` set one for the user of the session cookie, a user
    the view's authentication classes did not accept.
    :class:`RLSTenantPermission` refuses such a request.

    When the active context already is the user's (``RLSTenantMiddleware``
    set it for a session-authenticated user), the mixin enters nothing and
    leaves the response to Django to render, inside that context.

    Override :meth:`get_rls_context` to choose the context differently, for
    example to let an admin pick a tenant, or a user one of the tenants they
    belong to. :class:`RLSTenantPermission` accepts the tenant context it
    entered for the request.
    """

    # Set by dispatch() for the request: where the context is entered, whether
    # it was, and the holder of the RLS context it entered (if the context
    # manager set one up), which RLSTenantPermission accepts.
    _rls_exit_stack: ExitStack | None = None
    _rls_context_entered: bool = False
    _rls_entered_context: _Live | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a class that lists the mixin after ``APIView``, which would bypass it.

        Raises:
            RLSConfigurationError: If ``APIView`` precedes the mixin in the MRO.
        """
        super().__init_subclass__(**kwargs)
        from rest_framework.views import APIView  # noqa: PLC0415  -- see the module imports

        mro = cls.__mro__
        if APIView in mro and mro.index(APIView) < mro.index(RLSTenantAPIViewMixin):
            msg = (
                f"{cls.__qualname__} lists RLSTenantAPIViewMixin after a DRF view class, so "
                f"APIView.dispatch() runs instead of the mixin's and no RLS context is set. "
                f"List the mixin first: class {cls.__name__}(RLSTenantAPIViewMixin, ...)."
            )
            raise RLSConfigurationError(msg)

    def get_rls_context(self, request: Request) -> AbstractContextManager[None] | None:
        """Return the context to run this request in, or ``None`` to enter none.

        Called once per request, after authentication. The default returns
        ``None`` for an unauthenticated request and when the active context is
        already the one ``request.user`` implies, ``admin_context()`` for a
        tenant admin and ``tenant_context(user.rls_tenant_id)`` otherwise.

        An override is where a selected tenant is validated:
        :class:`RLSTenantPermission` accepts the tenant context it returns for
        this request, whichever tenant that is (``admin_context()`` only for a
        tenant admin).

        Args:
            request: The DRF request; ``request.user`` is authenticated.

        Raises:
            PermissionDenied: If a non-admin user has no tenant (fail-closed,
                like ``RLSTenantModelAdmin``).
        """
        user: Any = request.user
        if not _is_authenticated(user):
            return None
        wanted = _context_gucs_for(user)
        if wanted is None:
            logger.warning(
                "RLSTenantAPIViewMixin: %s %s is not a tenant admin and has no tenant; "
                "denying the request. Hint: %s",
                type(user).__name__,
                getattr(user, "pk", None),
                HINT_USER_NO_TENANT,
            )
            audit.denied("drf", "user_without_tenant", user_id=audit.user_id_of(user))
            raise PermissionDenied(_NO_TENANT_DETAIL)
        if _get_context_gucs() == wanted:
            return None
        if wanted == ADMIN_GUCS:
            return admin_context()
        return tenant_context(user.rls_tenant_id)

    def perform_authentication(self, request: Request) -> None:
        """Authenticate the request, then enter its RLS context."""
        super().perform_authentication(request)
        stack = self._rls_exit_stack
        if stack is None:
            # Called outside dispatch(), which owns the context's exit.
            return
        context = self.get_rls_context(request)
        if context is not None:
            enclosing = _current_live()
            stack.enter_context(audit.attributed(context, source="drf", user=request.user))
            self._rls_context_entered = True
            entered = _current_live()
            # A context manager that sets up no RLS context (``nullcontext()``)
            # leaves the enclosing one current: that is not the view's choice.
            if entered is not enclosing:
                self._rls_entered_context = entered

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        """Serve the request, and exit the RLS context ``perform_authentication()`` entered.

        A response that is rendered later (a DRF ``Response``) is rendered
        here, inside the context, so lazy data and the browsable API's forms
        are scoped too. Not while a transaction is marked for rollback (a
        handled error under ``ATOMIC_REQUESTS``), which refuses every query:
        Django then renders the response after the rollback, outside the
        context.
        """
        with ExitStack() as stack:
            self._rls_exit_stack = stack
            try:
                response = super().dispatch(request, *args, **kwargs)
                # Only a context the mixin entered ends here; otherwise Django
                # renders the response inside the enclosing one.
                if (
                    self._rls_context_entered
                    and isinstance(response, SimpleTemplateResponse)
                    and not _rollback_pending()
                ):
                    response.render()
                return response
            finally:
                self._rls_exit_stack = None
                self._rls_context_entered = False
                self._rls_entered_context = None


class RLSTenantPermission(BasePermission):
    """Allow a request only inside the RLS context its user implies, or its view chose.

    ``has_permission`` grants an authenticated request when an RLS context is
    active and, for a user who is not a tenant admin, it is a tenant context
    (never the admin bypass) that is either the one
    :class:`RLSTenantAPIViewMixin` entered for this request (what
    :meth:`~RLSTenantAPIViewMixin.get_rls_context` returned, such as a tenant
    the user selected and the override validated) or the user's own
    (``rls_tenant_id``). A tenant admin passes in any context. A view that
    forgot the mixin, served to a token client the middleware saw as
    anonymous, so answers 401/403 instead of an empty result.

    ``has_object_permission`` checks that the object's tenant foreign key
    (``RLS_TENANTS["TENANT_FK_FIELD"]``) is the active tenant; any object
    passes in the admin context, and so does an object without that field.
    PostgreSQL already filters what the view can read; this is defense in
    depth for objects obtained another way.

    Use it with the mixin, globally or per view::

        REST_FRAMEWORK = {
            "DEFAULT_PERMISSION_CLASSES": [
                "rest_framework.permissions.IsAuthenticated",
                "django_rls_tenants.contrib.drf.RLSTenantPermission",
            ],
        }
    """

    message = "This request is not running in its user's RLS context."

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return whether the active RLS context is the view's or the one ``request.user`` implies.

        The view's is the tenant context :class:`RLSTenantAPIViewMixin`
        entered for this request, while it is the active one. A refusal of an
        authenticated user is logged as a ``context.denied`` audit record; an
        unauthenticated request is not (DRF answers it 401).
        """
        user: Any = request.user
        if not _is_authenticated(user):
            return False
        active = _get_context_gucs()
        if active is None:
            audit.denied("drf", "no_context", user_id=audit.user_id_of(user))
            return False
        if user.is_tenant_admin or active == _context_gucs_for(user):
            return True
        # The admin bypass is a tenant admin's only.
        if active != ADMIN_GUCS and isinstance(view, RLSTenantAPIViewMixin):
            entered = view._rls_entered_context  # noqa: SLF001  -- the mixin's per-request state
            if entered is not None and entered is _current_live():
                return True
        audit.denied(
            "drf",
            "context_mismatch",
            user_id=audit.user_id_of(user),
            tenant_id=get_current_tenant_id(),
            admin=active == ADMIN_GUCS,
        )
        return False

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:  # noqa: ARG002  -- DRF signature
        """Return whether ``obj`` belongs to the active tenant (always, in the admin context).

        A refusal is logged as a ``context.denied`` audit record.
        """
        user_id = audit.user_id_of(request.user)
        active = _get_context_gucs()
        if active is None:
            audit.denied("drf", "no_context", user_id=user_id)
            return False
        if active == ADMIN_GUCS:
            return True
        try:
            field = obj._meta.get_field(rls_tenants_config.TENANT_FK_FIELD)  # noqa: SLF001
        except (AttributeError, FieldDoesNotExist):
            return True  # not a tenant-scoped model instance
        value = getattr(obj, field.attname, None)
        if value is not None and str(value) == active.current_tenant:
            return True
        audit.denied(
            "drf",
            "object_of_other_tenant",
            user_id=user_id,
            tenant_id=get_current_tenant_id(),
            admin=False,
            model=obj._meta.label,  # noqa: SLF001
        )
        return False


class CurrentTenantDefault:
    """Serializer field default: the tenant of the active RLS context.

    Like DRF's ``CurrentUserDefault``, for the tenant foreign key::

        tenant = serializers.HiddenField(default=CurrentTenantDefault())

    Returns an instance of ``RLS_TENANTS["TENANT_MODEL"]`` that holds only its
    primary key (its other fields load on first access), so filling the field
    costs no query.

    Raises:
        NoTenantContextError: When called outside a tenant context (no context,
            or the admin context, which has no tenant).
    """

    requires_context = True

    def __call__(self, serializer_field: serializers.Field[Any, Any, Any, Any]) -> Model:  # noqa: ARG002  -- DRF calls it with the field
        """Return the active tenant."""
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            admin = get_rls_context_active()
            audit.denied("drf", "no_context", admin=admin)
            state = "the admin context" if admin else "no RLS context"
            msg = (
                f"CurrentTenantDefault needs a tenant context, but the serializer is "
                f"validated in {state}."
            )
            raise NoTenantContextError(msg, hint=HINT_NO_CONTEXT)
        tenant_model = apps.get_model(rls_tenants_config.TENANT_MODEL)
        pk = tenant_model._meta.pk  # noqa: SLF001
        assert pk is not None  # noqa: S101  -- a concrete model always has one
        # A loaded-from-the-database instance with every other field deferred.
        tenant: Model = tenant_model.from_db(None, [pk.attname], [pk.to_python(tenant_id)])
        return tenant

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class RLSTenantSerializerMixin(_ModelSerializerBase):
    """``ModelSerializer`` mixin that takes the tenant foreign key from the RLS context.

    Combine it with ``ModelSerializer``, mixin first::

        class OrderSerializer(RLSTenantSerializerMixin, serializers.ModelSerializer):
            class Meta:
                model = Order
                fields = ["id", "product", "amount"]

    Outside the admin context (in a tenant context, or with no context at all)
    the tenant foreign key (``RLS_TENANTS["TENANT_FK_FIELD"]``) becomes
    ``HiddenField(default=CurrentTenantDefault())``, whatever ``Meta`` says: it
    is never in the output, a value in the input is ignored, and creates and
    updates set it to the active tenant (a partial update leaves it alone).
    ``UniqueConstraint``s that include it are still validated. In the admin
    context the serializer is left as declared, so an admin serializer that
    lists the field lets the admin choose the tenant. A model without the
    field is left alone. Other writable fields that would write the tenant
    foreign key (a declared ``tenant_id`` field, or one with
    ``source="tenant"``) are removed outside the admin context; read-only
    ones, such as ``CharField(source="tenant.name", read_only=True)``, stay.

    The fields are built on first access, inside the view, so the context the
    serializer sees is the request's.

    ``validate()`` then rejects a foreign key to a row of another tenant, which
    ``ModelSerializer`` never checks (it does not call ``full_clean()``); see
    :func:`~django_rls_tenants.tenants.models.validate_cross_tenant_fks`. It
    checks the foreign keys the data writes, against the tenant the row will
    have, and every foreign key of an object the data moves to another tenant.
    """

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        """Return the fields, with the tenant foreign key hidden outside the admin context."""
        fields = super().get_fields()
        if get_rls_context_active() and get_current_tenant_id() is None:
            return fields  # the admin context: as declared
        model: type[Model] = self.Meta.model
        try:
            tenant_fk = model._meta.get_field(rls_tenants_config.TENANT_FK_FIELD)  # noqa: SLF001
        except FieldDoesNotExist:
            return fields
        writes_tenant = {tenant_fk.name, getattr(tenant_fk, "attname", tenant_fk.name)}
        fields = {
            name: field
            for name, field in fields.items()
            if field.read_only or (field.source or name).split(".")[0] not in writes_tenant
        }
        fields[tenant_fk.name] = serializers.HiddenField(default=CurrentTenantDefault())
        return fields

    def to_internal_value(self, data: Any) -> Any:
        """Convert ``data``, keeping it for the errors of :meth:`validate`.

        Not ``initial_data``: a ``many=True`` child serializer has none.
        A primary key field that failed with ``does_not_exist`` (what it
        reports for a row of another tenant, which the auto-scope filter and
        RLS hide) is looked up for the ``fk.cross_tenant_rejected`` audit
        record; the error is unchanged.
        """
        self._rls_data = data
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as exc:
            self._audit_hidden_targets(data, exc.detail)
            raise

    def _audit_hidden_targets(self, data: Any, detail: Any) -> None:
        """Log the primary keys ``detail`` reports as not found that exist in another tenant.

        Only inside a tenant context, the only one in which the field's
        queryset hides another tenant's rows; the row's tenant is then the
        active one (the tenant field is hidden and filled from it).
        """
        from django_rls_tenants.tenants.models import (  # noqa: PLC0415  -- needs the app registry
            _checked_foreign_keys,
            _foreign_keys_of_other_tenants,
        )

        tenant_id = get_current_tenant_id()
        if tenant_id is None or not isinstance(detail, dict):
            return
        checked = _checked_foreign_keys(self.Meta.model)
        if checked is None:
            return
        by_name = {key: field for field in checked[1] for key in (field.name, field.attname)}
        rejected: dict[ForeignKey[Any, Any], Any] = {}
        for name, errors in detail.items():
            serializer_field = self.fields.get(name)
            if not isinstance(serializer_field, serializers.PrimaryKeyRelatedField) or not any(
                getattr(error, "code", None) == "does_not_exist" for error in errors
            ):
                continue
            field = by_name.get(serializer_field.source or name)
            if field is None or not field.target_field.primary_key:
                continue
            value = serializer_field.get_value(data)
            try:
                if serializer_field.pk_field is not None:
                    value = serializer_field.pk_field.to_internal_value(value)
                value = field.target_field.to_python(value)
            except (DjangoValidationError, serializers.ValidationError, TypeError, ValueError):
                continue
            if value is not None:
                rejected[field] = value
        if rejected:
            request = self.context.get("request")
            _foreign_keys_of_other_tenants(
                tenant_id,
                {},
                model=self.Meta.model,
                instance=self.instance if isinstance(self.instance, self.Meta.model) else None,
                source="drf",
                user_id=audit.user_id_of(getattr(request, "user", None)),
                rejected=rejected,
            )

    def validate(self, attrs: Any) -> Any:
        """Validate, then reject a foreign key to a row of another tenant.

        The row's tenant is the one the data writes, else the object's. Each
        foreign key to an RLS-protected model that the data writes is checked
        against it, and every such foreign key of the object when the data
        moves it to another tenant. A target's tenant is read in
        ``admin_context()``, one query per foreign key.

        The error is the one the serializer field gives an object that does
        not exist (``does_not_exist`` of a primary key, slug or hyperlink
        field, else the model field's ``invalid`` message), so it does not
        reveal that the row exists in another tenant.

        Raises:
            rest_framework.serializers.ValidationError: Keyed by serializer
                field name (``non_field_errors`` for a foreign key the
                serializer has no writable field for).
        """
        from django_rls_tenants.tenants.models import (  # noqa: PLC0415  -- needs the app registry
            _checked_foreign_keys,
            _foreign_keys_of_other_tenants,
        )

        attrs = super().validate(attrs)
        checked = _checked_foreign_keys(self.Meta.model)
        if checked is None:
            return attrs
        tenant_field, foreign_keys = checked
        instance = self.instance if isinstance(self.instance, self.Meta.model) else None
        tenant_written, tenant_id = _written_value(attrs, tenant_field)
        if not tenant_written and instance is not None:
            tenant_id = getattr(instance, tenant_field.attname)
        # The values passed the serializer fields; DRF reports a to_python() error.
        tenant_id = tenant_field.to_python(tenant_id)
        if tenant_id is None:
            return attrs
        moved = (
            tenant_written
            and instance is not None
            and tenant_id != getattr(instance, tenant_field.attname)
        )
        targets: dict[ForeignKey[Any, Any], Any] = {}
        for field in foreign_keys:
            written, value = _written_value(attrs, field)
            if not written:
                if not moved:
                    continue
                value = getattr(instance, field.attname)
            value = field.to_python(value)
            if value is not None:
                targets[field] = value
        if not targets:
            return attrs
        request = self.context.get("request")
        mismatched = _foreign_keys_of_other_tenants(
            tenant_id,
            targets,
            model=self.Meta.model,
            instance=instance,
            source="drf",
            user_id=audit.user_id_of(getattr(request, "user", None)),
        )
        if mismatched:
            sources = {
                declared.source: name
                for name, declared in self.fields.items()
                if not declared.read_only
            }
            errors: dict[str, list[ErrorDetail]] = {}
            for field in mismatched:
                name = sources.get(field.name) or sources.get(field.attname)
                key = api_settings.NON_FIELD_ERRORS_KEY if name is None else name
                serializer_field = None if name is None else self.fields[name]
                sent = (
                    empty
                    if serializer_field is None
                    else serializer_field.get_value(getattr(self, "_rls_data", {}))
                )
                errors.setdefault(key, []).extend(
                    _does_not_exist(serializer_field, field, targets[field], sent),
                )
            raise serializers.ValidationError(errors)
        return attrs


def _written_value(attrs: Any, field: ForeignKey[Any, Any]) -> tuple[bool, Any]:
    """Return whether ``attrs`` writes ``field``, and the ``attname`` value it writes.

    A value that is not an object (the data of a nested serializer, which a
    custom ``create()`` turns into a new row) is not a write of the field.
    """
    if field.name in attrs:
        related = attrs[field.name]
        if related is None:
            return True, None
        if not isinstance(related, Model):
            return False, None
        return True, getattr(related, field.target_field.attname)
    if field.attname in attrs:
        return True, attrs[field.attname]
    return False, None


def _does_not_exist(
    serializer_field: serializers.Field[Any, Any, Any, Any] | None,
    field: ForeignKey[Any, Any],
    value: Any,
    sent: Any = empty,
) -> list[ErrorDetail]:
    """Return the error ``serializer_field`` gives a target that does not exist.

    The message shows what the client sent, as DRF's does: the target's own
    primary key or slug would differ for input such as ``"03"``, and tell the
    client that the row exists.

    Args:
        serializer_field: The serializer field that writes ``field``, if any.
        field: The model's foreign key.
        value: The ``attname`` value the foreign key holds.
        sent: The field's value in the request data (``empty``: not sent, as
            for an object the data moves to another tenant).
    """
    from django_rls_tenants.tenants.models import _invalid_target  # noqa: PLC0415

    if isinstance(serializer_field, serializers.HyperlinkedRelatedField):
        shown: Any = value  # its message has no placeholder
    elif isinstance(serializer_field, serializers.PrimaryKeyRelatedField):
        shown = value if sent is empty else sent
    elif isinstance(serializer_field, serializers.SlugRelatedField) and sent is not empty:
        shown = smart_str(sent)
    else:
        return [ErrorDetail(_invalid_target(field, value).messages[0], code="invalid")]
    # As Field.fail() formats it; str.format() ignores the unused keywords.
    message = serializer_field.error_messages["does_not_exist"].format(
        pk_value=shown,
        slug_name=getattr(serializer_field, "slug_field", None),
        value=shown,
    )
    return [ErrorDetail(message, code="does_not_exist")]


class RLSTenantModelSerializer(RLSTenantSerializerMixin, serializers.ModelSerializer):  # type: ignore[type-arg]
    """``ModelSerializer`` with :class:`RLSTenantSerializerMixin` applied."""
