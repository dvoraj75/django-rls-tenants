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
  served outside the context its user implies (a view without the mixin, say),
  and an object of another tenant.
- :class:`RLSTenantSerializerMixin` / :class:`RLSTenantModelSerializer` -- a
  ``ModelSerializer`` that hides the tenant foreign key and fills it from the
  active context; :class:`CurrentTenantDefault` is its field default.

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
    from rest_framework.exceptions import PermissionDenied
    from rest_framework.permissions import BasePermission
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    msg = (
        "django_rls_tenants.contrib.drf requires Django REST Framework. "
        "Install it with: pip install django-rls-tenants[drf]"
    )
    raise ImportError(msg) from exc

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import connections
from django.template.response import SimpleTemplateResponse

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.errors import HINT_NO_CONTEXT, HINT_USER_NO_TENANT
from django_rls_tenants.tenants.state import (
    ADMIN_GUCS,
    ContextGucs,
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from django.db.models import Model
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase
    from rest_framework.request import Request
    from rest_framework.views import APIView

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
    example to let an admin pick a tenant.
    """

    # Set by dispatch() for the request: where the context is entered, and
    # whether it was.
    _rls_exit_stack: ExitStack | None = None
    _rls_context_entered: bool = False

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
            stack.enter_context(context)
            self._rls_context_entered = True

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


class RLSTenantPermission(BasePermission):
    """Allow a request only inside the RLS context its user implies.

    ``has_permission`` grants an authenticated request when an RLS context is
    active and, for a user who is not a tenant admin, it is exactly the
    user's own tenant context (never another tenant's, never the admin
    bypass). A tenant admin passes in any context. A view that forgot
    :class:`RLSTenantAPIViewMixin`, served to a token client the middleware
    saw as anonymous, so answers 401/403 instead of an empty result.

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

    def has_permission(self, request: Request, view: APIView) -> bool:  # noqa: ARG002  -- DRF signature
        """Return whether the active RLS context is the one ``request.user`` implies."""
        user: Any = request.user
        if not _is_authenticated(user):
            return False
        active = _get_context_gucs()
        if active is None:
            return False
        if user.is_tenant_admin:
            return True
        return active == _context_gucs_for(user)

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:  # noqa: ARG002  -- DRF signature
        """Return whether ``obj`` belongs to the active tenant (always, in the admin context)."""
        active = _get_context_gucs()
        if active is None:
            return False
        if active == ADMIN_GUCS:
            return True
        try:
            field = obj._meta.get_field(rls_tenants_config.TENANT_FK_FIELD)  # noqa: SLF001
        except (AttributeError, FieldDoesNotExist):
            return True  # not a tenant-scoped model instance
        value = getattr(obj, field.attname, None)
        return value is not None and str(value) == active.current_tenant


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
            state = "the admin context" if get_rls_context_active() else "no RLS context"
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


class RLSTenantModelSerializer(RLSTenantSerializerMixin, serializers.ModelSerializer):  # type: ignore[type-arg]
    """``ModelSerializer`` with :class:`RLSTenantSerializerMixin` applied."""
