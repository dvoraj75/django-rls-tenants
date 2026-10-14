"""Django middleware that sets RLS context per-request.

Provides ``RLSTenantMiddleware`` which reads the authenticated user
and sets GUC variables for the duration of each request, on Django's
synchronous (WSGI) path and on its own native async (ASGI) path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import ExitStack
from contextvars import ContextVar, Token
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections, transaction
from django.utils.module_loading import import_string

from django_rls_tenants.exceptions import RLSConfigurationError
from django_rls_tenants.rls.guc import clear_guc, set_guc
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _resolve_user_guc_vars
from django_rls_tenants.tenants.state import (
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from django.http import HttpRequest, HttpResponse, HttpResponseBase
    from django.utils.deprecation import MiddlewareMixin

    from django_rls_tenants.tenants.conf import RLSTenantsConfig
else:
    # Django 6.2 moves MiddlewareMixin to django.middleware and deprecates the
    # old import path (RemovedInDjango2029Warning). django-stubs still declares
    # it under django.utils.deprecation, hence the TYPE_CHECKING split.
    try:
        from django.middleware import MiddlewareMixin
    except ImportError:  # Django 5.2 - 6.1
        from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger("django_rls_tenants")


class _ThreadMarker:
    """Identity token for one thread; compared with ``is``."""

    __slots__ = ()


# Whether GUCs were set during this request: the marker of the thread that set
# them, or ``None``. A ``ContextVar`` gives each request its own value, in WSGI
# threads and in ASGI tasks alike; 1.1.0 replaced a ``threading.local`` flag,
# which the coroutines on one event-loop thread shared.
_gucs_set_var: ContextVar[_ThreadMarker | None] = ContextVar("rls_gucs_set", default=None)

# Holds one marker per thread (per-thread by design). ``sync_to_async`` copies
# the request's ``ContextVar``s into every hop, including the
# ``thread_sensitive=False`` hops that run on other threads. Those threads have
# connections of their own, which the request's cleanup never reaches (#130);
# the marker tells them apart from the thread that set the GUCs. Compared by
# identity, since a ``threading.get_ident()`` value can be reused once its
# thread has exited.
_thread_state = threading.local()


def _current_thread_marker() -> _ThreadMarker:
    """Return the current thread's marker, creating it on first use."""
    marker: _ThreadMarker | None = getattr(_thread_state, "marker", None)
    if marker is None:
        marker = _thread_state.marker = _ThreadMarker()
    return marker


def _mark_gucs_set(thread_marker: _ThreadMarker | None = None) -> None:
    """Mark that GUCs were set in the current context.

    Args:
        thread_marker: Marker of the thread that set them, from
            ``_current_thread_marker()``. Defaults to the current thread's.
    """
    _gucs_set_var.set(_current_thread_marker() if thread_marker is None else thread_marker)


def _clear_gucs_set_flag() -> None:
    """Clear the GUC flag in the current context."""
    _gucs_set_var.set(None)


def _were_gucs_set() -> bool:
    """Check whether GUCs were set in the current context."""
    return _gucs_set_var.get() is not None


def _were_gucs_set_on_this_thread() -> bool:
    """Check whether the current thread is the one that set the context's GUCs.

    ``False`` when no GUCs were set, and on any other thread, such as a
    ``thread_sensitive=False`` hop that only holds a copy of the request's
    ``ContextVar``s.
    """
    return _gucs_set_var.get() is _current_thread_marker()


def _reset_or_clear[T](
    token: Token[T] | None,
    reset: Callable[[Token[T]], None],
    clear: Callable[[], object],
) -> None:
    """Restore a ``ContextVar`` through its token, or clear it when the token is unusable.

    A token is unusable when ``process_request`` and ``process_response`` /
    ``process_exception`` ran in separate ``sync_to_async`` hops, each in a copy of
    the request's ``contextvars.Context``: asgiref copies the values set by one hop
    back into the request context, but the ``Token`` belongs to the discarded copy
    and ``ContextVar.reset()`` rejects it with ``ValueError``. That is how
    ``MiddlewareMixin.__acall__`` drove this middleware under ASGI before 1.4.0
    (#58), and how it still drives a subclass that overrides those hooks. The
    native ``__acall__`` keeps its tokens in the request coroutine (#38) and never
    needs this fallback. Clearing the variable restores the same
    "no context" baseline the token would have restored, because the middleware
    is the outermost context setter of a request. A ``RuntimeError`` (token used
    twice) is a real bug and is not caught.

    Args:
        token: The token from the matching ``set_*`` call, or ``None`` if the value
            was never set.
        reset: The ``state.reset_*`` function for the variable.
        clear: Callable that sets the variable to its default.
    """
    if isinstance(token, Token):
        try:
            reset(token)
        except ValueError:
            logger.debug(
                "ContextVar token belongs to another Context (ASGI hop); clearing %s instead",
                token.var.name,
            )
            clear()
        return
    clear()


def _is_authenticated(request: HttpRequest) -> bool:
    """Return ``True`` when the request carries an authenticated user.

    Evaluates the lazy ``request.user`` set by ``AuthenticationMiddleware``,
    which may hit the session store and the database.
    """
    return hasattr(request, "user") and bool(request.user.is_authenticated)


def _clear_gucs_on_all_databases(conf: Any) -> None:
    """Best-effort clear of GUCs on all configured database aliases.

    Used during error handling to prevent partial GUC state from
    leaking. Silently ignores failures on individual aliases.

    Args:
        conf: ``RLSTenantsConfig`` instance.
    """
    for db_alias in conf.DATABASES:
        try:
            clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
            clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
        except Exception:  # noqa: S110  -- best-effort cleanup, connection may be dead
            pass


def _clear_request_gucs(conf: RLSTenantsConfig) -> None:
    """Clear both GUCs on every configured alias at the end of a request.

    A no-op with ``USE_LOCAL_SET=True``: the ``SET LOCAL`` values vanish with
    the transaction that ends the request.

    Args:
        conf: ``RLSTenantsConfig`` instance.
    """
    if conf.USE_LOCAL_SET:
        return
    for db_alias in conf.DATABASES:
        clear_guc(conf.GUC_IS_ADMIN, using=db_alias)
        clear_guc(conf.GUC_CURRENT_TENANT, using=db_alias)
    logger.debug("Middleware cleared GUCs on databases=%s", conf.DATABASES)


class _RequestScope(NamedTuple):
    """State of a request whose GUCs the async enter hop has set."""

    tenant_id: int | str | None
    thread_marker: _ThreadMarker


_ASGI_LOCAL_SET_MESSAGE = (
    "RLSTenantMiddleware cannot use RLS_TENANTS['USE_LOCAL_SET'] = True "
    "under ASGI: SET LOCAL needs a transaction held open across the whole "
    "request, which the async middleware path cannot provide. Serve the "
    "application through WSGI, or set USE_LOCAL_SET=False."
)


class RLSTenantMiddleware(MiddlewareMixin):
    """Set RLS context for each authenticated request.

    For authenticated users: sets ``tenant_context`` or ``admin_context``
    based on the user's ``TenantUser`` protocol implementation.

    For unauthenticated requests: no context is set. RLS policies block
    all access to protected tables (fail-closed).

    This is API-agnostic -- works identically for REST, GraphQL, Django
    views, or any other request handler.

    Add to ``MIDDLEWARE``::

        MIDDLEWARE = [
            ...
            "django_rls_tenants.tenants.middleware.RLSTenantMiddleware",
        ]

    With ``USE_LOCAL_SET=True`` the middleware owns a database transaction
    for every authenticated request: ``transaction.atomic()`` is opened on
    each alias in ``RLS_TENANTS["DATABASES"]`` around the rest of the
    middleware chain and the view, so the ``SET LOCAL`` GUCs are in force
    for the whole request and vanish at commit/rollback. ``ATOMIC_REQUESTS``
    is not required (and is harmless -- it nests as a savepoint). This
    transaction-per-request mode is only available on the synchronous
    (WSGI) code path.

    Under ASGI, ``__acall__`` serves the request natively instead of
    ``MiddlewareMixin.__acall__``: one thread-sensitive hop resolves the user
    and sets the GUCs, the tenant ``ContextVar`` values are set and reset
    with their own tokens in the request coroutine, and one hop clears the
    GUCs at the end. A subclass that overrides ``process_request`` or
    ``process_response`` keeps the hook-based path, so its hooks still run.
    ``USE_LOCAL_SET=True`` stays WSGI-only.
    """

    def __init__(self, get_response: Callable[..., Any]) -> None:
        super().__init__(get_response)
        # The native async path does not call these hooks, so a subclass that
        # customises them is served through MiddlewareMixin.__acall__ instead.
        self._sync_hooks_overridden = any(
            getattr(type(self), hook) is not getattr(RLSTenantMiddleware, hook)
            for hook in ("process_request", "process_response")
        )
        if rls_tenants_config.USE_LOCAL_SET:
            self._validate_exception_middleware_order()

    def _validate_exception_middleware_order(self) -> None:
        """Reject handlers that could consume an exception before our rollback hook."""
        after_rls = False
        for path in settings.MIDDLEWARE:
            middleware = import_string(path)
            if isinstance(middleware, type) and isinstance(self, middleware):
                after_rls = True
            elif after_rls and not isinstance(middleware, type):
                # A factory can attach process_exception to the returned instance;
                # inspecting the factory itself cannot establish hook ordering.
                raise RLSConfigurationError(
                    f"USE_LOCAL_SET=True cannot verify exception hooks for middleware "
                    f"factory {path!r}. Place it before RLSTenantMiddleware in MIDDLEWARE, "
                    f"or use a middleware class with inspectable hooks."
                )
            elif after_rls and hasattr(middleware, "process_exception"):
                raise RLSConfigurationError(
                    f"USE_LOCAL_SET=True requires exception-handling middleware {path!r} "
                    f"to appear before RLSTenantMiddleware in MIDDLEWARE, so a handled "
                    f"view exception cannot skip the request transaction rollback."
                )

    def __call__(self, request: HttpRequest) -> Any:
        """Run the request, inside a transaction per alias when ``USE_LOCAL_SET``.

        Django's ``ATOMIC_REQUESTS`` wraps only the view callable, never
        ``process_request``, so ``SET LOCAL`` issued there would run in
        autocommit and fail. The middleware therefore opens the transaction
        itself, around ``process_request`` -> downstream chain ->
        ``process_response``. Unauthenticated requests get no transaction
        (no GUCs are set for them either).
        """
        if self.async_mode:
            return self.__acall__(request)
        conf = rls_tenants_config
        if not conf.USE_LOCAL_SET or not _is_authenticated(request):
            return super().__call__(request)
        with ExitStack() as stack:
            for db_alias in conf.DATABASES:
                stack.enter_context(transaction.atomic(using=db_alias))
            # Lets process_exception tell "our" transaction apart from one
            # opened by someone else (another middleware, a test wrapper).
            setattr(request, "_rls_owns_transaction", True)  # noqa: B010  -- dynamic attr on HttpRequest
            try:
                return super().__call__(request)
            finally:
                try:
                    if getattr(request, "_rls_rollback_transaction", False) is True:
                        for db_alias in conf.DATABASES:
                            if connections[db_alias].in_atomic_block:
                                transaction.set_rollback(True, using=db_alias)
                    self._cleanup_rls_state(request)
                finally:
                    setattr(request, "_rls_owns_transaction", False)  # noqa: B010 -- dynamic request marker

    async def __acall__(self, request: HttpRequest) -> HttpResponseBase:
        """Serve the request on Django's async (ASGI) path.

        Replaces ``MiddlewareMixin.__acall__``, which ran ``process_request`` and
        ``process_response`` in separate ``sync_to_async`` hops. Here one
        thread-sensitive hop resolves ``request.user`` and sets the GUCs on every
        alias. The tenant ``ContextVar`` values are then set in the request
        coroutine, so the downstream chain and async views see them, and the
        ``finally`` block resets them with their own tokens, which restores any
        outer value. A second hop clears the GUCs, unless ``process_exception``
        already ended the context for a failed view. Cleanup also runs when the
        request task is cancelled; only a second cancellation that arrives while
        the clear hop is still queued can skip it, and the ``request_finished``
        safety net then retries where it can see the request's flag. As on the
        WSGI path, the context ends when the chain returns, before a streaming
        response body is iterated.

        A subclass that overrides ``process_request`` or ``process_response``
        is served by ``_acall_through_sync_hooks`` instead, so its hooks run as
        they did before this path existed.

        ``USE_LOCAL_SET=True`` is not supported here: the async path cannot hold
        a synchronous ``transaction.atomic()`` open across the awaited chain, so
        authenticated requests raise ``RLSConfigurationError``.
        """
        if self._sync_hooks_overridden:
            return await self._acall_through_sync_hooks(request)
        conf = rls_tenants_config
        scope = await self._aenter_request(request, conf)
        if scope is None:
            return await self._aget_response(request)
        tenant_token = set_current_tenant_id(scope.tenant_id)
        active_token = set_rls_context_active(True)
        # The hop's thread, not the event loop's, owns the GUCs.
        _mark_gucs_set(scope.thread_marker)
        try:
            return await self._aget_response(request)
        finally:
            try:
                reset_current_tenant_id(tenant_token)
                reset_rls_context_active(active_token)
            finally:
                # False when process_exception has already cleared the GUCs. Left
                # set if the clear fails, so the request_finished safety net can
                # retry. Runs even if a reset fails, as in _cleanup_rls_state.
                if _were_gucs_set():
                    await sync_to_async(_clear_request_gucs, thread_sensitive=True)(conf)
                    _clear_gucs_set_flag()

    def _enter_request_gucs(
        self,
        request: HttpRequest,
        conf: RLSTenantsConfig,
    ) -> _RequestScope | None:
        """Resolve the request user and set the GUCs on every alias, in one sync call.

        ``__acall__`` runs this in a single thread-sensitive hop. It never sets
        a ``ContextVar``: ``__acall__`` does that in the request coroutine, so
        the tokens can be reset there (#58).

        Args:
            request: The Django HTTP request.
            conf: ``RLSTenantsConfig`` instance.

        Returns:
            ``None`` for an unauthenticated request (nothing was set), otherwise
            the auto-scope tenant ID (``None`` for an admin user) and the marker
            of the thread that set the GUCs.

        Raises:
            RLSConfigurationError: If ``USE_LOCAL_SET=True`` and the request is
                authenticated.
        """
        if not _is_authenticated(request):
            return None
        if conf.USE_LOCAL_SET:
            raise RLSConfigurationError(_ASGI_LOCAL_SET_MESSAGE)
        user: Any = request.user
        try:
            guc_vars = _resolve_user_guc_vars(user, conf)
            self._set_gucs_on_all_databases(guc_vars, conf)
            tenant_id = None if user.is_tenant_admin else user.rls_tenant_id
        except Exception:
            logger.exception("Failed to set RLS GUC variables, clearing to prevent leak")
            _clear_gucs_on_all_databases(conf)
            raise
        logger.debug(
            "Middleware set GUCs on databases=%s for user=%s (admin=%s, tenant=%s)",
            conf.DATABASES,
            type(user).__name__,
            user.is_tenant_admin,
            getattr(user, "rls_tenant_id", None),
        )
        return _RequestScope(tenant_id, _current_thread_marker())

    async def _aenter_request(
        self,
        request: HttpRequest,
        conf: RLSTenantsConfig,
    ) -> _RequestScope | None:
        """Run ``_enter_request_gucs`` in one hop, undoing it if the task is cancelled meanwhile.

        The hop is awaited directly, not wrapped in a task, so ``ContextVar``
        values set while ``request.user`` is resolved (by an authentication
        backend, for example) reach the rest of the request, as they did
        through ``process_request``. asgiref cannot stop a sync function that
        is already running in its thread: on cancellation it discards the result
        and raises at once. The GUCs the hop may have set are then cleared in a
        second hop before ``CancelledError`` propagates. That hop is queued on
        the same thread-sensitive thread, so it runs after the first one ends.
        """
        try:
            return await sync_to_async(self._enter_request_gucs, thread_sensitive=True)(
                request,
                conf,
            )
        except asyncio.CancelledError:
            await sync_to_async(_clear_gucs_on_all_databases, thread_sensitive=True)(conf)
            raise

    async def _acall_through_sync_hooks(self, request: HttpRequest) -> HttpResponseBase:
        """Serve the request through ``MiddlewareMixin.__acall__`` and the sync hooks.

        Used for subclasses that override ``process_request`` or
        ``process_response``. Each hook runs in its own ``sync_to_async`` hop,
        so cleanup clears the ``ContextVar`` values instead of resetting them
        (see ``_reset_or_clear``). A cancelled request skips Django's response
        and exception hooks, so the ``finally`` block cleans it up instead.

        Raises:
            RLSConfigurationError: If ``USE_LOCAL_SET=True`` and the request is
                authenticated.
        """
        if rls_tenants_config.USE_LOCAL_SET and await sync_to_async(
            _is_authenticated,
            thread_sensitive=True,
        )(request):
            raise RLSConfigurationError(_ASGI_LOCAL_SET_MESSAGE)
        try:
            return await super().__acall__(request)
        finally:
            if getattr(request, "_rls_state_reset", False) is not True or _were_gucs_set():
                await sync_to_async(self._cleanup_rls_state, thread_sensitive=True)(request)

    async def _aget_response(self, request: HttpRequest) -> HttpResponseBase:
        """Await the downstream chain; ``async_mode`` guarantees it is a coroutine function."""
        return await cast("Awaitable[HttpResponseBase]", self.get_response(request))

    def process_request(self, request: HttpRequest) -> None:
        """Set GUC variables on all configured databases.

        Iterates over ``RLS_TENANTS["DATABASES"]`` and sets GUCs on each
        alias. If setting GUCs on one database fails, clears all GUCs on
        databases that were already set, then re-raises. Used on the
        synchronous path only; ``__acall__`` uses ``_enter_request_gucs``.
        """
        if not _is_authenticated(request):
            return

        conf = rls_tenants_config
        user: Any = request.user

        # Track tokens so the except branch can reset via token (not
        # push a new value) when the ContextVar was already set before
        # the failure.
        tenant_token: Token[int | str | None] | None = None
        active_token: Token[bool] | None = None
        try:
            guc_vars = _resolve_user_guc_vars(user, conf)
            self._set_gucs_on_all_databases(guc_vars, conf)
            logger.debug(
                "Middleware set GUCs on databases=%s for user=%s (admin=%s, tenant=%s)",
                conf.DATABASES,
                type(user).__name__,
                user.is_tenant_admin,
                getattr(user, "rls_tenant_id", None),
            )

            # Set auto-scope state for RLSManager.get_queryset().
            # Use the original tenant ID from the user (preserving type)
            # rather than the string GUC representation, so that
            # get_current_tenant_id() returns a consistent type.
            if not user.is_tenant_admin:
                tenant_token = set_current_tenant_id(user.rls_tenant_id)
            else:
                tenant_token = set_current_tenant_id(None)
            setattr(request, "_rls_tenant_token", tenant_token)  # noqa: B010  -- dynamic attr on HttpRequest

            # Mark RLS context as active for strict mode. This
            # distinguishes "middleware set context" from "no context".
            active_token = set_rls_context_active(True)
            setattr(request, "_rls_context_active_token", active_token)  # noqa: B010  -- dynamic attr on HttpRequest
            _mark_gucs_set()
        except Exception:
            logger.exception("Failed to set RLS GUC variables, clearing to prevent leak")
            # Reset via token when available to avoid stacking unreset
            # ContextVar values; _reset_or_clear falls back to clearing when
            # the token was never created (error occurred before set call).
            _reset_or_clear(
                tenant_token,
                reset_current_tenant_id,
                partial(set_current_tenant_id, None),
            )
            _reset_or_clear(
                active_token,
                reset_rls_context_active,
                partial(set_rls_context_active, False),
            )
            setattr(request, "_rls_state_reset", True)  # noqa: B010 -- tokens already reset
            _clear_gucs_on_all_databases(conf)
            raise

    def process_response(
        self,
        request: HttpRequest,
        response: HttpResponse,
    ) -> HttpResponse:
        """Clear GUC variables and auto-scope state to prevent cross-request leaks."""
        self._cleanup_rls_state(request)
        return response

    def process_exception(
        self,
        request: HttpRequest,
        exception: Exception,  # noqa: ARG002  -- required by MiddlewareMixin
    ) -> None:
        """Record a view failure before Django converts it to an error response.

        Defer ``set_rollback(True)`` until the chain returns to ``__call__``.
        Setting it here would prevent error handlers and response middleware
        from querying even after a non-database exception such as ``Http404``.
        All writes, including those made by error handlers, are rolled back
        when the middleware-owned transaction exits. An explicit error
        response without an exception still commits.

        Without a middleware-owned transaction the hook ends the request
        context instead. Under ASGI, Django calls it in its own
        ``sync_to_async`` hop and then builds the error response on another
        thread and database connection; ending the context first keeps that
        error handling fail-closed, as on WSGI. ``__acall__`` then only resets
        its tokens.
        """
        if getattr(request, "_rls_owns_transaction", False) is True:
            setattr(request, "_rls_rollback_transaction", True)  # noqa: B010 -- dynamic request marker
        else:
            self._cleanup_rls_state(request)

    @staticmethod
    def _set_gucs_on_all_databases(guc_vars: dict[str, str], conf: Any) -> None:
        """Set GUC variables on all configured database aliases.

        If setting GUCs on one database fails, clears all GUCs on
        databases that were already set, then re-raises.

        Args:
            guc_vars: Mapping of GUC names to values.
            conf: ``RLSTenantsConfig`` instance.
        """
        completed_aliases: list[str] = []
        try:
            for db_alias in conf.DATABASES:
                for guc_name, guc_value in guc_vars.items():
                    if guc_value:
                        set_guc(
                            guc_name,
                            guc_value,
                            is_local=conf.USE_LOCAL_SET,
                            using=db_alias,
                        )
                    else:
                        clear_guc(guc_name, is_local=conf.USE_LOCAL_SET, using=db_alias)
                completed_aliases.append(db_alias)
        except Exception:
            # Rollback GUCs on databases that were already set.
            for done_alias in completed_aliases:
                try:
                    clear_guc(conf.GUC_IS_ADMIN, using=done_alias)
                    clear_guc(conf.GUC_CURRENT_TENANT, using=done_alias)
                except Exception:  # noqa: S110  -- best-effort cleanup
                    pass
            raise

    @staticmethod
    def _cleanup_rls_state(request: HttpRequest) -> None:
        """Reset ContextVars and clear GUCs.

        ContextVars are reset via their tokens, or cleared when the token
        comes from another ``contextvars.Context`` (ASGI, see
        ``_reset_or_clear``). Clears GUCs on all configured database aliases;
        the GUC clearing runs even if a reset fails. Skips database
        round-trips for requests where GUCs were never set (e.g.,
        unauthenticated requests, health checks).

        Args:
            request: The Django HTTP request being finalised.
        """
        try:
            # Django runs process_response even after process_exception. Tokens are
            # single-use, and resetting to defaults on a second pass would also lose
            # any outer context restored by the first pass.
            if getattr(request, "_rls_state_reset", False) is not True:
                # No token stored means an unauthenticated request or an error
                # during process_request before the token was saved: clear.
                _reset_or_clear(
                    getattr(request, "_rls_tenant_token", None),
                    reset_current_tenant_id,
                    partial(set_current_tenant_id, None),
                )
                _reset_or_clear(
                    getattr(request, "_rls_context_active_token", None),
                    reset_rls_context_active,
                    partial(set_rls_context_active, False),
                )
                setattr(request, "_rls_state_reset", True)  # noqa: B010 -- request cleanup marker
        finally:
            if _were_gucs_set():
                _clear_request_gucs(rls_tenants_config)
                _clear_gucs_set_flag()
