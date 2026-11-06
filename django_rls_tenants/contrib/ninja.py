"""Django Ninja integration: the RLS context for API operations.

Django Ninja authenticates inside the operation (its ``auth=`` callbacks), after
every Django middleware has run. A client that authenticates with a bearer
token, an API key or any other Ninja authentication therefore reaches
:class:`~django_rls_tenants.tenants.middleware.RLSTenantMiddleware` as an
anonymous user, and the middleware sets no context: the operation reads zero
rows (fail-closed) or, with ``STRICT_MODE``, raises. This module opens the
context where Ninja knows the user:

- :class:`RLSTenantNinjaAPI` -- a ``NinjaAPI`` that enters ``tenant_context()``
  / ``admin_context()`` (or their async counterparts) for the authenticated
  user right after Ninja authenticates the request, keeps it for the
  operation, the serialization of its result and the rendering of an error,
  and exits it before the operation's response is returned. It covers every
  router mounted on the API.
- :class:`RLSTenantGuard` -- a view decorator that refuses a request served
  outside the context its user implies (an API that is not an
  :class:`RLSTenantNinjaAPI`, say) or the tenant context the API entered for
  it, and checks an object's tenant (:meth:`RLSTenantGuard.has_object_permission`).
- :class:`RLSTenantModelSchema` -- a ``ModelSchema`` without the model's
  tenant field, and :func:`rls_save` / :func:`arls_save`, which write a
  payload to an instance with the tenant taken from the active context and
  reject a foreign key to a row that is not the tenant's.

Ninja sets no hook between its authentication and the operation besides its
decorators, so :class:`RLSTenantNinjaAPI` registers two with
``add_decorator()``: one in ``mode="view"``, which (despite the name) wraps
the operation's ``run()``, owns the exit, and one in ``mode="operation"``,
which wraps the view function and enters the context; ``run()`` calls it
after the authentication, the throttles and the input validation.

**Import order.** Unlike DRF's ``DEFAULT_PERMISSION_CLASSES``, no Ninja
setting names a class that Ninja imports while its own modules load:
``ninja/conf.py`` only reads ``NINJA_*`` values when ``ninja`` is imported,
and ``NINJA_PAGINATION_CLASS`` is resolved with ``import_string()`` when an
operation is paginated. So this module imports Ninja at module level. It
needs configured settings and the app registry, as ``ninja`` and the
library's ``tenants`` package do.

**Limitation: streaming.** A streaming operation (``response=JSONL[...]`` or
``SSE[...]``, a generator or async generator view) is consumed after
``run()`` returns, outside any context ``run()`` could keep open (and after
``RLSTenantMiddleware``'s has ended). The API passes such a view through
without a context, so it reads zero rows unless the generator opens a
context itself. Do not put a streaming operation under
:class:`RLSTenantGuard`: it refuses a ``def`` one with a 403 before the
response starts, and an ``async def`` one only when the stream starts (the
response has begun). Mount streaming operations on a router or API without
``rls_guard=``.

This is an **optional** integration. It imports :mod:`ninja`, which is not a
dependency of the core library; install it with ``pip install
django-rls-tenants[ninja]``. Nothing in ``tenants/`` or ``rls/`` imports this
module, and it is intentionally **not** re-exported from the top-level package.
"""

from __future__ import annotations

import functools
import inspect
import logging
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, NamedTuple, cast

try:
    from ninja import ModelSchema, NinjaAPI
    from ninja import responses as ninja_responses
    from ninja.errors import HttpError
    from ninja.errors import ValidationError as NinjaValidationError
    from ninja.orm.metaclass import ModelSchemaMetaclass
    from ninja.schema import ResolverMetaclass
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    msg = (
        "django_rls_tenants.contrib.ninja requires Django Ninja. "
        "Install it with: pip install django-rls-tenants[ninja]"
    )
    raise ImportError(msg) from exc

from asgiref.sync import sync_to_async
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import ForeignKey, Model, QuerySet
from pydantic import BaseModel

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import (
    _no_rls_state,
    _settle_after_no_context,
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
)
from django_rls_tenants.tenants.errors import HINT_NO_CONTEXT, HINT_USER_NO_TENANT
from django_rls_tenants.tenants.selection import (
    _may_select,
    _request_tenant_id,
    _selects_tenants,
    _vary_on_tenant_header,
)
from django_rls_tenants.tenants.state import (
    ADMIN_GUCS,
    ContextGucs,
    _current_live,
    _get_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
)
from django_rls_tenants.tenants.types import TenantUser

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from contextlib import AbstractAsyncContextManager, AbstractContextManager

    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

    from django_rls_tenants.tenants.state import _Live

__all__ = [
    "RLSTenantGuard",
    "RLSTenantModelSchema",
    "RLSTenantNinjaAPI",
    "arls_save",
    "rls_save",
]

logger = logging.getLogger("django_rls_tenants")

# The 403 an API client sees for a non-admin user without a tenant. The hint,
# which names the TenantUser attributes, goes to the log instead.
_NO_TENANT_DETAIL = "Your account is not assigned to a tenant."

# The per-request state RLSTenantNinjaAPI keeps on the request (_RequestState).
_STATE_ATTRIBUTE = "_rls_ninja_state"

# _RequestState.user before the API has looked the user up.
_UNRESOLVED: Any = object()

# ninja.responses.Status (an operation's ``Status(code, body)`` result); older
# Ninja releases only take a ``(code, body)`` tuple.
_STATUS: type | None = getattr(ninja_responses, "Status", None)


class _RequestState:
    """What :class:`RLSTenantNinjaAPI` keeps on a request while ``run()`` serves it.

    Attributes:
        stack: Where the context is entered; ``run()``'s wrapper closes it.
        user: The user the context is chosen for, once looked up.
        entered: The holder of the RLS context the API entered, if entering
            it made a new holder current (:class:`RLSTenantGuard` accepts it).
    """

    __slots__ = ("entered", "stack", "user")

    def __init__(self, stack: ExitStack | AsyncExitStack) -> None:
        self.stack = stack
        self.user: Any = _UNRESOLVED
        self.entered: _Live | None = None


class _Choice(NamedTuple):
    """The context a request runs in: the admin bypass, the tenant ``tenant_id``, or none.

    ``no_context`` is the no-context block (``admin`` and ``tenant_id`` unused):
    no RLS context at all, whatever context encloses the request.
    """

    admin: bool
    tenant_id: Any
    no_context: bool = False


# The choice for a client an ``auth=`` callback authenticated that is no user.
_NO_CONTEXT = _Choice(admin=False, tenant_id=None, no_context=True)


@asynccontextmanager
async def _async_no_rls_state() -> AsyncIterator[None]:
    """``_no_rls_state()`` for an ``async def`` operation, entered in its coroutine.

    The block's ``ContextVar`` values are set and reset in the coroutine. Its
    exit settles the configured connections of the event loop's thread, which
    holds none, so the settle runs again in a thread-sensitive hop after the
    reset, as ``@with_rls_context``'s async wrapper does (the hop sets no
    ``ContextVar``, #58).
    """
    try:
        with _no_rls_state():
            yield
    finally:
        await sync_to_async(_settle_after_no_context, thread_sensitive=True)()


def _state_of(request: HttpRequest) -> _RequestState | None:
    """Return the state :class:`RLSTenantNinjaAPI` keeps on ``request``, if it serves it."""
    state = getattr(request, _STATE_ATTRIBUTE, None)
    return state if isinstance(state, _RequestState) else None


def _default_rls_user(request: HttpRequest) -> Any:
    """Return the user an ``auth=`` callback authenticated, else the session's.

    ``request.auth`` if it is a ``TenantUser``, ``None`` if it is anything
    else, and ``request.user`` (``None`` without ``AuthenticationMiddleware``)
    only when no ``auth=`` callback authenticated the request. Ninja sets
    ``request.auth`` only to a callback's truthy result (its test client sets
    it to ``None``). Fail-closed: the client a callback authenticated (an API
    key, say) is not the user of the session cookie a cross-site request
    carries along, and Ninja runs no CSRF check for such a callback. For that
    ``None``, :class:`RLSTenantNinjaAPI` runs the operation with no RLS
    context, also inside one ``RLSTenantMiddleware`` set for the session's
    user.
    """
    auth = getattr(request, "auth", None)
    if not auth:
        return getattr(request, "user", None)
    return auth if isinstance(auth, TenantUser) else None


def _rls_user_of(request: HttpRequest) -> Any:
    """Return the user :class:`RLSTenantNinjaAPI` chose the context for, else the default."""
    state = _state_of(request)
    if state is not None and state.user is not _UNRESOLVED:
        return state.user
    return _default_rls_user(request)


def _is_authenticated(user: Any) -> bool:
    """Return whether ``user`` is an authenticated user.

    An object an ``auth=`` callback returned that has no ``is_authenticated``
    attribute is one: Ninja authenticated it.
    """
    return user is not None and bool(getattr(user, "is_authenticated", True))


def _context_gucs_for(user: Any, tenant_id: Any = None) -> ContextGucs | None:
    """Return the GUC values of the context ``user`` implies; ``None`` for a tenantless non-admin.

    Args:
        user: An authenticated user satisfying the ``TenantUser`` protocol.
        tenant_id: The tenant a non-admin user's request selected, used
            instead of ``user.rls_tenant_id``; ``None`` for that default.
    """
    if user.is_tenant_admin:
        return ADMIN_GUCS
    if tenant_id is None:
        tenant_id = user.rls_tenant_id
    return None if tenant_id is None else ContextGucs.for_tenant(tenant_id)


def _is_streaming(view_func: Callable[..., Any]) -> bool:
    """Return whether ``view_func`` is a generator view, which Ninja consumes after ``run()``.

    Looks through ``functools.wraps`` wrappers (``__wrapped__``): a decorator
    such as :class:`RLSTenantGuard` turns a ``def`` generator view into a
    plain function that returns the generator.
    """
    view = inspect.unwrap(view_func)
    return inspect.isgeneratorfunction(view) or inspect.isasyncgenfunction(view)


async def _evaluated(result: Any) -> Any:
    """Evaluate the queryset an ``async def`` operation returned, in the active context.

    Ninja serializes an ``async def`` operation's result synchronously, in the
    event loop, where evaluating a queryset raises ``SynchronousOnlyOperation``.
    Django's async iteration of a queryset runs its own ``_fetch_all()`` in
    one thread-sensitive hop, which copies the coroutine's ``ContextVar`` values
    (the context the API entered, or the one enclosing it), and keeps the
    rows in the queryset's result cache. Returning the same queryset keeps
    everything its evaluation does (``for_user()``'s scope, the prefetches
    with their tenant filter, ``STRICT_MODE``), and Ninja serializes the
    cached rows. A queryset in a ``Status`` (or Ninja's deprecated
    ``(status, body)`` tuple) is evaluated too. Anything else is returned as
    it is, so these still raise ``SynchronousOnlyOperation`` (a 500, with no
    row read): a relation the serialization reaches lazily (not in
    ``select_related()`` / ``prefetch_related()``), a returned ``Manager``
    (return ``.all()``), a ``RawQuerySet``, and a queryset nested in a dict,
    a list or a schema (evaluate it in the operation).
    """
    body = result
    if _STATUS is not None and isinstance(result, _STATUS):
        body = cast("Any", result).value
    elif isinstance(result, tuple) and len(result) == 2:  # noqa: PLR2004  -- (status, body)
        body = result[1]
    if isinstance(body, QuerySet):
        async for _row in body:
            pass
    return result


class RLSTenantNinjaAPI(NinjaAPI):
    """A ``NinjaAPI`` whose operations run inside the RLS context of their user.

    Use it instead of ``NinjaAPI``::

        from django_rls_tenants.contrib.ninja import RLSTenantGuard, RLSTenantNinjaAPI
        from ninja.security import django_auth

        api = RLSTenantNinjaAPI(auth=django_auth, rls_guard=RLSTenantGuard())
        api.add_router("/orders/", orders_router)

    Right after Ninja authenticates the request (and runs the throttles and
    validates the input), the API enters ``admin_context()`` for a user with
    ``is_tenant_admin`` and ``tenant_context(user.rls_tenant_id)`` for any
    other user (the :class:`~django_rls_tenants.tenants.types.TenantUser`
    protocol), or, for a
    :class:`~django_rls_tenants.tenants.types.MultiTenantUser`, the tenant
    the request selects among the user's (see :meth:`get_rls_context`). An
    ``async def`` operation gets ``async_admin_context()`` /
    ``async_tenant_context()``, entered in the request's coroutine. The
    context stays active for the operation, the serialization of what it
    returns (a lazy queryset included) and the rendering of an error, and is
    exited before the response leaves the API, also when the operation
    raises. A non-admin user without a tenant gets a 403, a tenant the user
    may not select the 403 of
    :data:`~django_rls_tenants.tenants.selection.NOT_SELECTABLE_MESSAGE`.

    The user is ``request.auth`` when the ``auth=`` callback returned a
    ``TenantUser``, nobody when it returned anything else (an API key
    object: override :meth:`get_rls_user` to map it to its user), and
    ``request.user`` only for an operation without ``auth=``
    (:meth:`get_rls_user`). When an ``auth=`` callback authenticated the
    request but :meth:`get_rls_user` names no authenticated user, the API
    runs the operation with no RLS context, whatever context encloses it (one
    ``RLSTenantMiddleware`` set for the user of the session cookie included):
    it reads no protected row, and ``STRICT_MODE`` raises (fail-closed). For
    a request no ``auth=`` callback authenticated and no session user, the
    API enters nothing: the operation runs in whatever context encloses it,
    which is none unless other code set one. :class:`RLSTenantGuard` refuses
    both. A request whose ``auth=`` callback fails gets Ninja's 401 before
    anything is entered.

    When the active context already is the user's (``RLSTenantMiddleware``
    set it for a session-authenticated user), the API enters nothing.

    Args:
        rls_guard: An :class:`RLSTenantGuard` to apply to every operation,
            inside the context. ``None`` (the default) applies none.
        **kwargs: The ``NinjaAPI`` arguments.

    Decorators added later with ``add_decorator()`` (the API's or a
    router's) wrap these: they run before the context is entered and after
    it is exited. A streaming operation gets no context (see the module
    documentation).

    Override :meth:`get_rls_context` and :meth:`aget_rls_context` together to
    choose the context differently (the first serves ``def`` operations, the
    second ``async def`` ones). :class:`RLSTenantGuard` accepts the tenant
    context they return.
    """

    def __init__(self, *, rls_guard: RLSTenantGuard | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Ninja applies decorators in the order they were added: the first is
        # the innermost. "operation" wraps the view function, "view" wraps run().
        if rls_guard is not None:
            self.add_decorator(rls_guard, mode="operation")
        self.add_decorator(self._rls_enter, mode="operation")
        self.add_decorator(self._rls_own_exit, mode="view")

    def get_rls_user(self, request: HttpRequest) -> Any:
        """Return the user to choose the request's context for.

        ``request.auth`` when the ``auth=`` callback returned an object that
        satisfies the ``TenantUser`` protocol. ``None`` when it returned
        anything else: the operation then runs with no RLS context, whatever
        context encloses it, and :class:`RLSTenantGuard` refuses it.
        Override this method to map such a result (a token or API key
        object) to its user. The default never falls back to the session's
        user then, because a cross-site request carries the victim's session
        cookie along with an attacker's key, and Ninja checks CSRF only for
        cookie authentication (``APIKeyCookie``, ``django_auth``). Only an
        operation without ``auth=`` gets ``request.user`` (``None`` without
        ``AuthenticationMiddleware``), served without a CSRF check (Ninja's
        views are ``csrf_exempt``).

        Synchronous: an ``async def`` operation calls it in a
        ``sync_to_async`` hop, since ``request.user`` may query the session.
        The guard reads the user this method returned for the request.
        """
        return _default_rls_user(request)

    def get_rls_context(self, request: HttpRequest) -> AbstractContextManager[None] | None:
        """Return the context to run a ``def`` operation in, or ``None`` to enter none.

        Called once per request, after Ninja authenticated it. The default
        returns a no-context block when an ``auth=`` callback authenticated
        the request but :meth:`get_rls_user` names no authenticated user (the
        operation runs with no RLS context, whatever context encloses it:
        fail-closed), ``None`` for a request no ``auth=`` callback
        authenticated and no session user, and when the active context is
        already the one the user implies, ``admin_context()`` for a tenant
        admin and ``tenant_context(user.rls_tenant_id)`` otherwise.
        For a :class:`~django_rls_tenants.tenants.types.MultiTenantUser` who
        is not a tenant admin, the tenant is the one the request selects (the
        ``RLS_TENANTS["TENANT_HEADER"]`` header, then the session's
        :func:`~django_rls_tenants.tenants.selection.switch_tenant` selection),
        once it is checked to be one of the user's, else ``rls_tenant_id``.

        An override is where a selected tenant is validated:
        :class:`RLSTenantGuard` accepts the tenant context it returns for this
        request, whichever tenant that is (``admin_context()`` only for a
        tenant admin). Override :meth:`aget_rls_context` the same way.

        Args:
            request: The request; the ``auth=`` callbacks have run.

        Raises:
            ninja.errors.HttpError: 403 if a non-admin user has no tenant
                (fail-closed), or a ``MultiTenantUser``'s request selects a
                tenant that is not one of theirs.
        """
        choice = self._rls_choice(request)
        if choice is None:
            return None
        if choice.no_context:
            return _no_rls_state()
        if choice.admin:
            return admin_context()
        return tenant_context(choice.tenant_id)

    async def aget_rls_context(
        self,
        request: HttpRequest,
    ) -> AbstractAsyncContextManager[None] | None:
        """Return the context to run an ``async def`` operation in, or ``None`` to enter none.

        The same choice as :meth:`get_rls_context`, made in one thread-sensitive
        ``sync_to_async`` hop (the user lookup, the session and
        ``rls_tenant_ids`` may query), returning ``async_admin_context()``,
        ``async_tenant_context()`` or the async no-context block (its exit
        puts the enclosing context back in a thread-sensitive hop), which the
        API enters in the request's coroutine.

        Raises:
            ninja.errors.HttpError: As :meth:`get_rls_context`.
        """
        choice = await sync_to_async(self._rls_choice)(request)
        if choice is None:
            return None
        if choice.no_context:
            return _async_no_rls_state()
        if choice.admin:
            return async_admin_context()
        return async_tenant_context(choice.tenant_id)

    def _rls_choice(self, request: HttpRequest) -> _Choice | None:
        """Return the context the request's user implies, ``None`` if there is nothing to enter.

        Records the user on the request's state. Sets no ``ContextVar``: the
        async path calls it in a ``sync_to_async`` hop.

        Raises:
            ninja.errors.HttpError: As :meth:`get_rls_context`.
        """
        user = self.get_rls_user(request)
        state = _state_of(request)
        if state is not None:
            state.user = user
        if not _is_authenticated(user):
            # A client an auth= callback authenticated that is no user (an API key
            # get_rls_user() does not map): never the enclosing context, which
            # RLSTenantMiddleware may have set for the session cookie's user.
            return _NO_CONTEXT if getattr(request, "auth", None) else None
        if _selects_tenants(user):
            try:
                tenant_id = _request_tenant_id(request, user, source="ninja")
            except DjangoPermissionDenied as exc:
                raise HttpError(403, str(exc)) from exc
        else:
            tenant_id = user.rls_tenant_id
        wanted = _context_gucs_for(user, tenant_id)
        if wanted is None:
            logger.warning(
                "RLSTenantNinjaAPI: %s %s is not a tenant admin and has no tenant; "
                "denying the request. Hint: %s",
                type(user).__name__,
                getattr(user, "pk", None),
                HINT_USER_NO_TENANT,
            )
            audit.denied("ninja", "user_without_tenant", user_id=audit.user_id_of(user))
            raise HttpError(403, _NO_TENANT_DETAIL)
        if _get_context_gucs() == wanted:
            return None
        return _Choice(admin=wanted == ADMIN_GUCS, tenant_id=tenant_id)

    def _rls_enter(self, view_func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap an operation's view function to enter its RLS context first (``mode="operation"``).

        A generator view (a streaming operation), also one a decorator such as
        :class:`RLSTenantGuard` wraps, is returned as it is: Ninja consumes it
        after ``run()`` returns, when no context it entered is open. A queryset
        an ``async def`` view returns is evaluated before the wrapper returns
        it (:func:`_evaluated`).
        """
        if _is_streaming(view_func):
            return view_func
        if inspect.iscoroutinefunction(view_func):

            @functools.wraps(view_func)
            async def async_view(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
                await self._aenter_rls_context(request)
                return await _evaluated(await view_func(request, *args, **kwargs))

            return async_view

        @functools.wraps(view_func)
        def view(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            self._enter_rls_context(request)
            return view_func(request, *args, **kwargs)

        return view

    def _enter_rls_context(self, request: HttpRequest) -> None:
        """Enter the context of a ``def`` operation on the stack ``run()``'s wrapper closes."""
        state = _state_of(request)
        if state is None:
            # Called outside run(), whose wrapper owns the context's exit.
            return
        context = self.get_rls_context(request)
        # The guard reads the user get_rls_user() names, also when an override
        # of get_rls_context() enters nothing without looking it up.
        if state.user is _UNRESOLVED:
            state.user = self.get_rls_user(request)
        if context is None:
            return
        enclosing = _current_live()
        state.stack.enter_context(audit.attributed(context, source="ninja", user=state.user))
        entered = _current_live()
        # A context manager that sets up no RLS context (``nullcontext()``)
        # leaves the enclosing one current: that is not the API's choice.
        if entered is not enclosing:
            state.entered = entered

    async def _aenter_rls_context(self, request: HttpRequest) -> None:
        """Enter the context of an ``async def`` operation, in the request's coroutine."""
        state = _state_of(request)
        if state is None or not isinstance(state.stack, AsyncExitStack):
            # Called outside run(), whose wrapper owns the context's exit.
            return
        context = await self.aget_rls_context(request)
        if state.user is _UNRESOLVED:
            state.user = await sync_to_async(self.get_rls_user)(request)
        if context is None:
            return
        enclosing = _current_live()
        await state.stack.enter_async_context(
            audit.async_attributed(context, source="ninja", user=state.user),
        )
        entered = _current_live()
        if entered is not enclosing:
            state.entered = entered

    def _rls_own_exit(self, run: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap an operation's ``run()`` to exit the context it entered (``mode="view"``).

        ``run()`` authenticates, validates, calls the view function, serializes
        the result and renders an error; the context the view function's
        wrapper entered is exited when it returns or raises. The response
        gets the tenant header in ``Vary`` when the tenant could depend on it.
        """
        if inspect.iscoroutinefunction(run):

            @functools.wraps(run)
            async def async_run(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
                async with AsyncExitStack() as stack:
                    previous = getattr(request, _STATE_ATTRIBUTE, None)
                    setattr(request, _STATE_ATTRIBUTE, _RequestState(stack))
                    try:
                        response: HttpResponseBase = await run(request, *args, **kwargs)
                        _vary_on_tenant_header(request, response)
                        return response
                    finally:
                        setattr(request, _STATE_ATTRIBUTE, previous)

            return async_run

        @functools.wraps(run)
        def sync_run(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            with ExitStack() as stack:
                previous = getattr(request, _STATE_ATTRIBUTE, None)
                setattr(request, _STATE_ATTRIBUTE, _RequestState(stack))
                try:
                    response: HttpResponseBase = run(request, *args, **kwargs)
                    _vary_on_tenant_header(request, response)
                    return response
                finally:
                    setattr(request, _STATE_ATTRIBUTE, previous)

        return sync_run


class RLSTenantGuard:
    """Allow a request only inside the RLS context its user implies, or its API chose.

    A decorator for an operation's view function, ``def`` or ``async def``,
    listed under the route decorator::

        guard = RLSTenantGuard()

        @router.get("/orders/", response=list[OrderOut])
        @guard
        def list_orders(request):
            return Order.objects.all()

    or applied to every operation of an API with
    ``RLSTenantNinjaAPI(rls_guard=RLSTenantGuard())``. It runs inside the
    context the API entered, and refuses an unauthenticated request with a
    401 and a request :meth:`has_permission` denies with a 403
    (:attr:`message`). The operation then answers 401/403 instead of an
    empty result when it is served outside its user's context (a plain
    ``NinjaAPI`` a token client reached as an anonymous user of the
    middleware, say). In an ``async def`` operation the check runs in a
    ``sync_to_async`` hop.

    Do not apply it to a streaming operation: such an operation runs in no
    context (see the module documentation), so the guard refuses it, a
    ``def`` generator view with a clean 403 when Ninja calls it, an
    ``async`` generator only when the response starts streaming, which a
    refusal ends with an error. Mount streaming operations on a router or
    API without ``rls_guard=``.

    The user is the one :class:`RLSTenantNinjaAPI` chose the context for;
    without one, the same default as
    :meth:`RLSTenantNinjaAPI.get_rls_user`: ``request.auth`` if it is a
    ``TenantUser``, nobody (a 401) if an ``auth=`` callback returned
    anything else, ``request.user`` if none authenticated the request.
    """

    message = "This request is not running in its user's RLS context."

    def __call__[F: Callable[..., Any]](self, view_func: F) -> F:
        """Return ``view_func`` wrapped with the check.

        An ``async def`` view stays a coroutine function and an ``async``
        generator view an ``async`` generator function (Ninja picks the
        async operation from the view's kind). Any ``def`` view, a generator
        view included, becomes a plain function that checks, then calls it;
        :class:`RLSTenantNinjaAPI` still finds a generator view through
        ``__wrapped__``.
        """
        if inspect.isasyncgenfunction(view_func):

            @functools.wraps(view_func)
            async def async_stream(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
                await sync_to_async(self._check)(request)
                async for item in view_func(request, *args, **kwargs):
                    yield item

            return cast("F", async_stream)
        if inspect.iscoroutinefunction(view_func):

            @functools.wraps(view_func)
            async def async_view(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
                await sync_to_async(self._check)(request)
                return await view_func(request, *args, **kwargs)

            return cast("F", async_view)

        @functools.wraps(view_func)
        def view(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            # A generator view's generator is created only once the check passed:
            # Ninja calls this before the streaming response starts.
            self._check(request)
            return view_func(request, *args, **kwargs)

        return cast("F", view)

    def _check(self, request: HttpRequest) -> None:
        """Refuse a request :meth:`has_permission` denies.

        Raises:
            ninja.errors.HttpError: 401 for an unauthenticated request, 403
                with :attr:`message` for any other refusal.
        """
        if not _is_authenticated(_rls_user_of(request)):
            raise HttpError(401, "Unauthorized")
        if not self.has_permission(request):
            raise HttpError(403, self.message)

    def has_permission(self, request: HttpRequest) -> bool:
        """Return whether the active RLS context is the API's or the one the user implies.

        Grants an authenticated request when an RLS context is active and,
        for a user who is not a tenant admin, it is a tenant context (never
        the admin bypass) that is either the one :class:`RLSTenantNinjaAPI`
        entered for this request (what its ``get_rls_context()`` returned,
        such as a tenant the user selected and an override validated), while
        it is the active one, the user's own (``rls_tenant_id``), or, for a
        :class:`~django_rls_tenants.tenants.types.MultiTenantUser`, one of
        ``rls_tenant_ids``. A tenant admin passes in any context. A refusal
        of an authenticated user is logged as a ``context.denied`` audit
        record; an unauthenticated request is not.
        """
        user = _rls_user_of(request)
        if not _is_authenticated(user):
            return False
        active = _get_context_gucs()
        if active is None:
            audit.denied("ninja", "no_context", user_id=audit.user_id_of(user))
            return False
        if user.is_tenant_admin or active == _context_gucs_for(user):
            return True
        # The admin bypass is a tenant admin's only.
        if active != ADMIN_GUCS:
            state = _state_of(request)
            entered = None if state is None else state.entered
            if entered is not None and entered is _current_live():
                return True
            if _selects_tenants(user) and _may_select(user, active.current_tenant) is not None:
                return True
        audit.denied(
            "ninja",
            "context_mismatch",
            user_id=audit.user_id_of(user),
            tenant_id=get_current_tenant_id(),
            admin=active == ADMIN_GUCS,
        )
        return False

    def has_object_permission(self, request: HttpRequest, obj: Any) -> bool:
        """Return whether ``obj`` belongs to the active tenant (always, in the admin context).

        Checks the object's tenant foreign key
        (``RLS_TENANTS["TENANT_FK_FIELD"]``); an object without that field
        passes. PostgreSQL already filters what the operation can read; this
        is defense in depth for objects obtained another way. Call it in the
        operation (in a ``sync_to_async`` hop from an ``async def`` one if the
        user is not loaded yet). A refusal is logged as a ``context.denied``
        audit record.
        """
        user_id = audit.user_id_of(_rls_user_of(request))
        active = _get_context_gucs()
        if active is None:
            audit.denied("ninja", "no_context", user_id=user_id)
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
            "ninja",
            "object_of_other_tenant",
            user_id=user_id,
            tenant_id=get_current_tenant_id(),
            admin=False,
            model=obj._meta.label,  # noqa: SLF001
        )
        return False


# Set once RLSTenantModelSchema exists; its own class statement has no model.
_rls_model_schema: type[ModelSchema] | None = None


def _schema_model(meta: Any) -> type[Model] | None:
    """Return the model a schema's ``Meta`` names, ``None`` if it names none (Ninja reports it)."""
    model = getattr(meta, "model", None)
    if isinstance(model, str):
        model = apps.get_model(model)
    return model if isinstance(model, type) and issubclass(model, Model) else None


def _meta_without(meta: type, tenant_field: ForeignKey[Any, Any]) -> type:
    """Return a ``Meta`` like ``meta`` whose ``fields`` / ``exclude`` omit ``tenant_field``."""
    fields = getattr(meta, "fields", None)
    exclude = getattr(meta, "exclude", None)
    if fields == "__all__" or (not fields and exclude):
        excluded = [exclude] if isinstance(exclude, str) else list(exclude or [])
        if tenant_field.name not in excluded:
            excluded.append(tenant_field.name)
        overrides: dict[str, Any] = {"fields": None, "exclude": excluded}
    elif fields:
        dropped = {tenant_field.name, tenant_field.attname}
        overrides = {"fields": [name for name in fields if name not in dropped]}
    else:
        return meta  # Ninja refuses a Meta with neither
    # Pydantic ignores a class attribute only if it is a class defined in the class body.
    overrides.update(__module__=meta.__module__, __qualname__=meta.__qualname__)
    return type(meta.__name__, (meta,), overrides)


class _RLSTenantModelSchemaMetaclass(ModelSchemaMetaclass):
    """Build an :class:`RLSTenantModelSchema` subclass: a ``ModelSchema`` without the tenant field.

    Ninja's metaclass builds the schema only for a class that lists
    ``ModelSchema`` itself among its bases, so this one adds it, after
    rewriting ``Meta`` to leave the tenant field out.
    """

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        if _rls_model_schema is None:
            # RLSTenantModelSchema itself, which has no Meta: no schema to build.
            return ResolverMetaclass.__new__(mcs, name, bases, namespace, **kwargs)
        if _rls_model_schema not in bases:
            # A subclass of a built schema, or the class Ninja generates for one:
            # Ninja does not rebuild it, but its body could declare the tenant field.
            schema = super().__new__(mcs, name, bases, namespace, **kwargs)
            meta = _inherited_meta(schema)
            model = None if meta is None else _schema_model(meta)
            if model is not None:
                _refuse_declared_tenant_field(name, schema, _model_tenant_field(model))
            return schema
        meta = namespace.get("Meta")
        model = None if meta is None else _schema_model(meta)
        tenant_field = None if model is None else _model_tenant_field(model)
        if meta is not None and tenant_field is not None:
            namespace = {**namespace, "Meta": _meta_without(meta, tenant_field)}
        schema = super().__new__(mcs, name, (*bases, ModelSchema), namespace, **kwargs)
        _refuse_declared_tenant_field(name, schema, tenant_field)
        return schema


def _inherited_meta(schema: type) -> Any:
    """Return the ``Meta`` of the nearest base of ``schema`` that declares one, else ``None``.

    The ``Meta`` Ninja built the schema from: one in the subclass's own body is
    not read (Ninja builds only a direct ``ModelSchema`` subclass).
    """
    for base in schema.__mro__[1:]:
        meta = vars(base).get("Meta")
        if meta is not None:
            return meta
    return None


def _refuse_declared_tenant_field(
    name: str,
    schema: type[BaseModel],
    tenant_field: ForeignKey[Any, Any] | None,
) -> None:
    """Refuse a schema with a field named, or aliased, like the model's tenant field.

    Raises:
        RLSConfigurationError: If ``schema`` has such a field.
    """
    if tenant_field is None:
        return
    declared = {tenant_field.name, tenant_field.attname} & _schema_keys(schema)
    if declared:
        msg = (
            f"{name} declares {sorted(declared)}, which would write or show the tenant "
            f"field {tenant_field.name!r} of {tenant_field.model.__name__}. "
            f"RLSTenantModelSchema never reads it from a payload: remove the field "
            f"(rls_save() sets the tenant from the active context), or use a plain "
            f"ModelSchema for an admin-only schema."
        )
        raise RLSConfigurationError(msg)


def _model_tenant_field(model: type[Model]) -> ForeignKey[Any, Any] | None:
    """Return the foreign key ``model``'s RLS policy filters on, or ``None``."""
    from django_rls_tenants.tenants.models import _tenant_field  # noqa: PLC0415  -- needs the app registry

    return _tenant_field(model)


def _schema_keys(schema: type[BaseModel]) -> set[str]:
    """Return the names and string aliases of ``schema``'s fields."""
    keys: set[str] = set()
    for field_name, info in schema.model_fields.items():
        keys.add(field_name)
        for alias in (info.alias, info.validation_alias, info.serialization_alias):
            if isinstance(alias, str):
                keys.add(alias)
    return keys


class RLSTenantModelSchema(ModelSchema, metaclass=_RLSTenantModelSchemaMetaclass):
    """``ModelSchema`` without the model's tenant field.

    Subclass it as you would ``ModelSchema``::

        class OrderIn(RLSTenantModelSchema):
            class Meta:
                model = Order
                fields = "__all__"

    The tenant field (the foreign key the model's ``RLSConstraint`` or
    ``RLS_TENANTS["EXTERNAL_MODELS"]`` entry names) is left out whatever
    ``Meta.fields`` / ``Meta.exclude`` say, so a payload never sets it and a
    response never shows it; a field declared in the class body under its
    name or ``attname`` raises :class:`RLSConfigurationError`. A model without
    a tenant field is left alone. Write a payload with :func:`rls_save`,
    which sets the tenant from the active context and checks the payload's
    foreign keys (Ninja checks their type, not that the row exists).

    Only a direct subclass is built from its ``Meta``, as with
    ``ModelSchema``. A subclass of a built schema keeps its fields, and
    declaring the tenant field in its body raises
    :class:`RLSConfigurationError` too.
    """


_rls_model_schema = RLSTenantModelSchema


def rls_save(
    request: HttpRequest,
    instance: Model,
    payload: BaseModel | Mapping[str, Any],
    *,
    exclude_unset: bool = False,
    loc: tuple[str | int, ...] = ("body", "payload"),
) -> Model:
    """Write ``payload`` to ``instance`` and save it, in the active tenant.

    ``payload`` is a schema instance (dumped with ``model_dump()``) or a
    mapping. Each key that names a concrete field of the model (by name or
    ``attname``; a foreign key's value is the target's key) is set on
    ``instance``, except the primary key and the tenant field, which are
    never taken from a payload; other keys, many-to-many fields included,
    are ignored. In a tenant context the tenant field is set to the active
    tenant; in the admin context it is left as the caller set it.

    Then every foreign key of ``instance`` to an RLS-protected model is
    checked against the instance's tenant (one query per foreign key, in
    ``admin_context()``), and the instance is saved. Ninja does not check
    that a foreign key's target exists, so a missing target and a row of
    another tenant get the same error, with the value the payload sent: it
    does not reveal that the row exists elsewhere. An existing target of
    another tenant is logged as an ``fk.cross_tenant_rejected`` audit record.

    Use :func:`arls_save` in an ``async def`` operation.

    Args:
        request: The request, for the user of the audit records.
        instance: The row to write, new or loaded in the active context.
        payload: The validated input.
        exclude_unset: Passed to ``model_dump()``: ``True`` writes only the
            fields the client sent (a partial update).
        loc: The location prefix of the errors, Ninja's for the operation's
            body argument (``("body", "<argument name>")``).

    Returns:
        ``instance``, saved.

    Raises:
        ninja.errors.ValidationError: For each foreign key whose target is
            not a row of the instance's tenant; Ninja answers 422.
        NoTenantContextError: If neither the active context (no context, or
            the admin context) nor ``instance`` gives the row a tenant.
    """
    from django_rls_tenants.tenants.models import (  # noqa: PLC0415  -- needs the app registry
        _checked_foreign_keys,
    )

    model = type(instance)
    data = (
        payload.model_dump(exclude_unset=exclude_unset)
        if isinstance(payload, BaseModel)
        else dict(payload)
    )
    checked = _checked_foreign_keys(model)
    written = _write_payload(instance, data, None if checked is None else checked[0])
    if checked is not None:
        tenant_field, foreign_keys = checked
        user_id = audit.user_id_of(_rls_user_of(request))
        tenant_id = _row_tenant(instance, tenant_field, user_id=user_id)
        # Each written field's key as the client sent it (a schema's alias) and value.
        input_keys = _input_keys(payload)
        sent = {name: (input_keys.get(key, key), data[key]) for name, key in written.items()}
        errors = _foreign_key_errors(
            instance,
            tenant_id,
            foreign_keys,
            sent,
            loc=loc,
            user_id=user_id,
        )
        if errors:
            raise NinjaValidationError(errors)
    instance.save()
    return instance


def _write_payload(
    instance: Model,
    data: Mapping[str, Any],
    tenant_field: ForeignKey[Any, Any] | None,
) -> dict[str, str]:
    """Set ``data``'s concrete fields on ``instance``, except the primary key and the tenant field.

    Returns:
        The key each field was written under, by field name.
    """
    never_written = set() if tenant_field is None else {tenant_field.name, tenant_field.attname}
    by_key = {
        key: field
        for field in instance._meta.concrete_fields  # noqa: SLF001
        if not field.primary_key
        for key in (field.name, field.attname)
    }
    written: dict[str, str] = {}
    for key, value in data.items():
        field = by_key.get(key)
        if field is None or key in never_written:
            continue
        if field.is_relation and not isinstance(value, Model):
            setattr(instance, field.attname, value)
        else:
            setattr(instance, field.name, value)
        written[field.name] = key
    return written


def _row_tenant(instance: Model, tenant_field: ForeignKey[Any, Any], *, user_id: Any) -> Any:
    """Set ``instance``'s tenant to the active tenant, if any; return the row's tenant.

    Raises:
        NoTenantContextError: If the row has no tenant either way.
    """
    active_tenant = get_current_tenant_id()
    if active_tenant is not None:
        setattr(instance, tenant_field.attname, tenant_field.to_python(active_tenant))
    tenant_id = tenant_field.to_python(getattr(instance, tenant_field.attname))
    if tenant_id is None:
        admin = get_rls_context_active()
        audit.denied("ninja", "no_context", user_id=user_id, admin=True if admin else None)
        state = "the admin context" if admin else "no RLS context"
        msg = (
            f"rls_save() needs a tenant context, or an instance with a tenant, but the "
            f"{type(instance).__name__} has none and the payload is saved in {state}."
        )
        raise NoTenantContextError(msg, hint=HINT_NO_CONTEXT)
    return tenant_id


def _foreign_key_errors(
    instance: Model,
    tenant_id: Any,
    foreign_keys: list[ForeignKey[Any, Any]],
    sent: Mapping[str, tuple[str, Any]],
    *,
    loc: tuple[str | int, ...],
    user_id: Any,
) -> list[dict[str, Any]]:
    """Return the errors of ``instance``'s foreign keys whose target is not a row of ``tenant_id``.

    A missing target and another tenant's get the same error, which shows the
    value the payload sent (else the instance's value).

    Args:
        instance: The row, with the payload written to it.
        tenant_id: The row's tenant.
        foreign_keys: Its foreign keys to RLS-protected models.
        sent: By field name, the key and value of each field the payload wrote.
        loc: The location prefix of the errors.
        user_id: The primary key of the user, for the audit record.
    """
    from django_rls_tenants.tenants.models import (  # noqa: PLC0415  -- needs the app registry
        _foreign_keys_of_other_tenants,
        _invalid_target,
    )

    errors: list[dict[str, Any]] = []
    targets: dict[ForeignKey[Any, Any], Any] = {}
    shown: dict[ForeignKey[Any, Any], Any] = {}
    for field in foreign_keys:
        key, sent_value = sent.get(field.name, (field.name, None))
        current = getattr(instance, field.attname)
        try:
            value = field.to_python(current)
        except DjangoValidationError as error:
            errors.append(_error(loc, key, error.messages[0]))
            continue
        if value is None:
            continue  # no target, or one that is not saved yet
        targets[field] = value
        shown[field] = (
            current if sent_value is None or isinstance(sent_value, Model) else sent_value
        )
    if targets:
        for field in _foreign_keys_of_other_tenants(
            tenant_id,
            targets,
            model=type(instance),
            instance=instance,
            source="ninja",
            user_id=user_id,
        ):
            message = _invalid_target(field, shown[field]).messages[0]
            key = sent.get(field.name, (field.name, None))[0]
            errors.append(_error(loc, key, message))
    return errors


def _input_keys(payload: BaseModel | Mapping[str, Any]) -> dict[str, str]:
    """Return, for a schema, each field's input key (its validation alias) by field name."""
    if not isinstance(payload, BaseModel):
        return {}
    keys: dict[str, str] = {}
    for name, info in type(payload).model_fields.items():
        alias = info.validation_alias if isinstance(info.validation_alias, str) else info.alias
        if isinstance(alias, str):
            keys[name] = alias
    return keys


def _error(loc: tuple[str | int, ...], key: str, message: str) -> dict[str, Any]:
    """Return a Ninja validation error item for the payload key ``key``."""
    return {"type": "invalid", "loc": [*loc, key], "msg": message}


async def arls_save(
    request: HttpRequest,
    instance: Model,
    payload: BaseModel | Mapping[str, Any],
    *,
    exclude_unset: bool = False,
    loc: tuple[str | int, ...] = ("body", "payload"),
) -> Model:
    """Async counterpart of :func:`rls_save`: the whole of it in one thread-sensitive hop."""
    return await sync_to_async(rls_save)(
        request,
        instance,
        payload,
        exclude_unset=exclude_unset,
        loc=loc,
    )
