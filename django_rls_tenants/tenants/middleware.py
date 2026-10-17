"""Django middleware that sets RLS context per-request.

Provides ``RLSTenantMiddleware`` which reads the authenticated user
and sets GUC variables for the duration of each request, on Django's
synchronous (WSGI) path and on its own native async (ASGI) path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar, Token
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections, transaction
from django.utils.module_loading import import_string

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.affinity import suspect_sessions, write_gucs
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import _context_gucs_of, _resolve_user_guc_vars
from django_rls_tenants.tenants.selection import (
    _request_tenant_id,
    _selects_tenants,
    _vary_on_tenant_header,
)
from django_rls_tenants.tenants.state import (
    _ContextGucsToken,
    _current_live,
    _end_if_newer,
    _hold_context_gucs,
    _release_context_gucs,
    _reset_context_gucs,
    _set_context_gucs,
    _values_of,
    get_current_tenant_id,
    get_rls_context_active,
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from django.http import HttpRequest, HttpResponse, HttpResponseBase
    from django.utils.deprecation import MiddlewareMixin

    from django_rls_tenants.tenants.conf import RLSTenantsConfig
    from django_rls_tenants.tenants.state import ContextGucs, _Live
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


def _end_context_gucs(entered: _ContextGucsToken | None) -> None:
    """End the request's context for the GUC affinity guard, and restore the outer one.

    Left live, the request's values would make the guard apply its GUCs again
    after the cleanup has cleared them, on this thread's later statements and,
    under WSGI, in later requests. The cleanup therefore does this first.
    :func:`state._reset_context_gucs` ends the context before it resets the
    ``ContextVar``, so even a reset that fails leaves it ended. A token from
    another ``Context`` (a hook, or Django's ``process_exception``, run in a
    ``sync_to_async`` hop of its own, see ``_reset_or_clear``) cannot reset
    the variable: the ended holder stays current there, and asgiref leaves it
    in the request too. The request so keeps a holder, which a cancelled call's
    block cannot replace (see :class:`state._Live`); a ``None`` copied back would
    let one in. A request that never set its values (an anonymous one) leaves
    an enclosing context alone.

    Args:
        entered: What ``_set_context_gucs()`` returned, or ``None`` if the
            values were never set.
    """
    if not isinstance(entered, _ContextGucsToken):
        return
    with suppress(ValueError):
        _reset_context_gucs(entered)


class _FoundContext(NamedTuple):
    """The context holder a request found when it started (``None`` for none)."""

    live: _Live | None


@contextmanager
def _holder_for_the_request(request: HttpRequest) -> Iterator[None]:
    """Hold a context holder for a request, and restore the one it found.

    The request keeps the context it is served in, or gets a holder of no
    context if there is none: without one, a ``sync_to_async`` call that the
    request (or a coroutine its view runs with ``async_to_sync``) cancels
    while the call's function is inside a context block would leave that
    block's context in the request (see :class:`state._Live`). On exit
    the holder found on entry is current again, whatever a hop of the request
    handed back (``process_request``'s holder, on the hook path), and a context
    block the view left open is ended (``state._release_context_gucs``).
    """
    held = _hold_context_gucs()
    setattr(request, "_rls_found_context", _FoundContext(held.live))  # noqa: B010  -- dynamic attr on HttpRequest
    try:
        yield
    finally:
        _release_context_gucs(held)


def _served_inside_a_context(request: HttpRequest) -> bool:
    """Return whether the request started inside a live context (a test's block, say)."""
    found = getattr(request, "_rls_found_context", None)
    return isinstance(found, _FoundContext) and _values_of(found.live) is not None


def _end_blocks_left_open(request: HttpRequest) -> None:
    """End a context block the view left open, before Django builds the error response.

    A view that keeps a generator suspended inside a context block leaves that
    block's context current in the request, and Django builds the error
    response in a copy of the request's context (under ASGI, on a worker
    thread, where the GUC affinity guard would apply it). A holder current
    there that is newer than the one the request found, and not the request's
    own context, is such a block, or a block that a middleware listed after
    this one holds around the rest of the chain: it cannot tell the two apart,
    and ends both (fail-closed). Called by ``process_exception`` and, for the
    errors it does not see, from Django's ``got_request_exception`` (``apps.py``).
    """
    found = getattr(request, "_rls_found_context", None)
    if not isinstance(found, _FoundContext):
        return
    current = _current_live()
    own = getattr(request, "_rls_context_gucs_token", None)
    if isinstance(own, _ContextGucsToken) and current is own.live:
        return
    _end_if_newer(current, found.live)


def _is_authenticated(request: HttpRequest) -> bool:
    """Return ``True`` when the request carries an authenticated user.

    Evaluates the lazy ``request.user`` set by ``AuthenticationMiddleware``,
    which may hit the session store and the database.
    """
    return hasattr(request, "user") and bool(request.user.is_authenticated)


def _clear_alias_gucs(db_alias: str) -> None:
    """Clear both GUCs on the thread's connection of ``db_alias`` in one statement.

    ``write_gucs()`` records the clear for the GUC affinity guard.
    """
    write_gucs(connections[db_alias], None)


def _clear_gucs_on_all_databases(conf: Any) -> None:
    """Best-effort clear of GUCs on all configured database aliases.

    Used during error handling to prevent partial GUC state from
    leaking. Silently ignores failures on individual aliases.

    Args:
        conf: ``RLSTenantsConfig`` instance.
    """
    for db_alias in conf.DATABASES:
        # Best-effort cleanup: the connection may be dead.
        with suppress(Exception):
            _clear_alias_gucs(db_alias)


def _clear_gucs_on_open_databases(conf: RLSTenantsConfig) -> None:
    """Best-effort clear of GUCs on the configured aliases this thread has a connection open on.

    For a request cancelled while its ``process_request`` hop may still be
    writing the GUCs (see ``_acall_through_sync_hooks``): the hop's connection is
    open on this thread; an alias it never touched is left closed.

    Args:
        conf: ``RLSTenantsConfig`` instance.
    """
    for db_alias in conf.DATABASES:
        if connections[db_alias].connection is None:
            continue
        with suppress(Exception):  # best-effort cleanup
            _clear_alias_gucs(db_alias)


def _clear_request_gucs(conf: RLSTenantsConfig) -> None:
    """Clear both GUCs on every configured alias at the end of a request.

    With ``USE_LOCAL_SET=True`` no statement runs: transaction-local values
    end with their transactions (the request's own, on the synchronous path),
    and what the sessions hold for themselves only counts as unknown from now
    on (``affinity.suspect_sessions()``), since the view may have written the
    GUCs for the session.

    Args:
        conf: ``RLSTenantsConfig`` instance.
    """
    if conf.USE_LOCAL_SET:
        suspect_sessions()
        return
    for db_alias in conf.DATABASES:
        _clear_alias_gucs(db_alias)
    logger.debug("Middleware cleared GUCs on databases=%s", conf.DATABASES)


def _resolve_request_user(
    user: Any,
    conf: RLSTenantsConfig,
    member: _Member | None,
) -> dict[str, str]:
    """Return the GUC values of the request's user; log ``context.denied`` for a tenantless one.

    Args:
        user: The request's authenticated user.
        conf: ``RLSTenantsConfig`` instance.
        member: What ``_member_of()`` returned for the request.

    Raises:
        NoTenantContextError: If a non-admin user has ``rls_tenant_id=None``.
    """
    try:
        return _resolve_user_guc_vars(
            user,
            conf,
            tenant_id=None if member is None else member.tenant_id,
        )
    except NoTenantContextError:
        audit.denied("middleware", "user_without_tenant", user_id=audit.user_id_of(user))
        raise


class _Member(NamedTuple):
    """The tenant the request of a user who selects tenants runs in (``None``: none)."""

    tenant_id: Any


def _member_of(request: HttpRequest, user: Any) -> _Member | None:
    """Return the tenant of a ``MultiTenantUser``'s request; ``None`` for any other user.

    Called before anything is set, so a refused selection is a 403 with
    nothing to undo.

    Raises:
        PermissionDenied: If the request selects a tenant that is not one of
            the user's (``selection._request_tenant_id()``).
    """
    if not _selects_tenants(user):
        return None
    return _Member(_request_tenant_id(request, user, source="middleware"))


def _log_member_without_tenant(user: Any) -> None:
    """Log that a ``MultiTenantUser`` without a selection or a default gets no context."""
    logger.debug(
        "Middleware set no RLS context for user=%s: it selected no tenant and has no "
        "rls_tenant_id (fail-closed)",
        type(user).__name__,
    )


def _scope_tenant_id(user: Any, member: _Member | None) -> Any:
    """Return the auto-scope tenant of the request: ``None`` for an admin.

    The tenant ID keeps its Python type (not the string GUC form; a selected
    one has the tenant primary key's type), so that
    ``get_current_tenant_id()`` returns a consistent type.
    """
    if member is not None:
        return member.tenant_id
    return None if user.is_tenant_admin else user.rls_tenant_id


class _AuditedContext(NamedTuple):
    """The request context a ``context.entered`` record was logged for."""

    tenant_id: int | str | None
    admin: bool
    user_id: Any


def _audit_request_entered(
    request: HttpRequest,
    tenant_id: int | str | None,
    *,
    admin: bool,
    user_id: Any,
) -> None:
    """Log ``context.entered`` for the request, and remember it for ``_audit_request_exited``."""
    entered = _AuditedContext(tenant_id, admin, user_id)
    audit.emit(audit.CONTEXT_ENTERED, source="middleware", **entered._asdict())
    setattr(request, "_rls_audited_context", entered)  # noqa: B010  -- dynamic attr on HttpRequest


def _audit_request_exited(request: HttpRequest) -> None:
    """Log ``context.exited`` for the request's context, once, if it was entered.

    Called wherever the request's context ends: the cleanup
    (``process_response``, ``process_exception``, a ``BaseException`` on the
    sync path) and the end of the native async path.
    """
    entered = getattr(request, "_rls_audited_context", None)
    if not isinstance(entered, _AuditedContext):
        return
    setattr(request, "_rls_audited_context", None)  # noqa: B010  -- dynamic attr on HttpRequest
    audit.emit(audit.CONTEXT_EXITED, source="middleware", **entered._asdict())


class _RequestScope(NamedTuple):
    """State of a request whose GUCs the async enter hop has set."""

    tenant_id: int | str | None
    gucs: ContextGucs
    thread_marker: _ThreadMarker
    user_id: Any


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
    for every authenticated request on the synchronous (WSGI) path:
    ``transaction.atomic()`` is opened on each alias in
    ``RLS_TENANTS["DATABASES"]`` around the rest of the middleware chain and
    the view, so the ``SET LOCAL`` GUCs are in force for the whole request and
    vanish at commit/rollback. ``ATOMIC_REQUESTS`` is not required (and is
    harmless -- it nests as a savepoint).

    Under ASGI, ``__acall__`` serves the request natively instead of
    ``MiddlewareMixin.__acall__``: one thread-sensitive hop resolves the user
    and sets the GUCs, the tenant ``ContextVar`` values are set and reset
    with their own tokens in the request coroutine, and one hop clears the
    GUCs at the end. A subclass that overrides ``process_request`` or
    ``process_response`` keeps the hook-based path, so its hooks still run.
    With ``USE_LOCAL_SET=True`` a request on the async path gets no
    transaction (none can be held open across the awaited chain; under ASGI
    with a synchronous-only middleware after this one, Django runs it on the
    synchronous path, with the transaction) and the middleware writes no GUC:
    the GUC affinity guard sets the request's values transaction-locally for
    each statement, giving a statement run in autocommit a transaction of its
    own (#96).
    """

    def __init__(self, get_response: Callable[..., Any]) -> None:
        super().__init__(get_response)
        # The native async path does not call these hooks, so a subclass that
        # customises them is served through MiddlewareMixin.__acall__ instead.
        self._sync_hooks_overridden = any(
            getattr(type(self), hook) is not getattr(RLSTenantMiddleware, hook)
            for hook in ("process_request", "process_response")
        )
        # Only the synchronous path opens the request transaction this protects.
        if rls_tenants_config.USE_LOCAL_SET and not self.async_mode:
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
        """Serve the request: on the WSGI path here, on the ASGI path in ``__acall__``.

        The WSGI request holds a context holder for its duration (see
        ``_holder_for_the_request``), which a coroutine the view runs with
        ``async_to_sync`` inherits.
        """
        if self.async_mode:
            return self.__acall__(request)
        with _holder_for_the_request(request):
            return self._call_sync(request)

    def _call_sync(self, request: HttpRequest) -> Any:
        """Run a request on the sync path; with ``USE_LOCAL_SET``, in a transaction per alias.

        Django's ``ATOMIC_REQUESTS`` wraps only the view callable, never
        ``process_request``, so ``SET LOCAL`` issued there would run in
        autocommit and fail. The middleware therefore opens the transaction
        itself, around ``process_request`` -> downstream chain ->
        ``process_response``. Unauthenticated requests get no transaction
        (no GUCs are set for them either).
        """
        conf = rls_tenants_config
        if not conf.USE_LOCAL_SET or not _is_authenticated(request):
            try:
                return super().__call__(request)
            except BaseException:
                # Django turns an Exception from the chain into a response, so
                # this is a BaseException (KeyboardInterrupt, a server's
                # timeout) that skipped process_response, or a failure in it.
                # Under WSGI the thread's next request would start inside this
                # request's context; the GUC affinity guard would apply it.
                with suppress(Exception):  # the original error wins
                    self._cleanup_rls_state(request)
                raise
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

        With ``USE_LOCAL_SET=True`` the async path opens no transaction (it
        cannot hold a synchronous ``transaction.atomic()`` open across the
        awaited chain) and writes no GUC; the GUC affinity guard applies the
        request's context to each statement, transaction-locally.
        """
        if self._sync_hooks_overridden:
            return await self._acall_through_sync_hooks(request)
        conf = rls_tenants_config
        scope = await self._aenter_request(request, conf)
        if scope is None:
            with _holder_for_the_request(request):
                response = await self._aget_response(request)
            _vary_on_tenant_header(request, response)
            return response
        setattr(request, "_rls_found_context", _FoundContext(_current_live()))  # noqa: B010  -- dynamic attr on HttpRequest
        tenant_token = set_current_tenant_id(scope.tenant_id)
        active_token = set_rls_context_active(True)
        gucs_token = _set_context_gucs(scope.gucs)
        # For process_exception, which ends the context from a hop of its own.
        setattr(request, "_rls_context_gucs_token", gucs_token)  # noqa: B010  -- dynamic attr on HttpRequest
        # The hop's thread, not the event loop's, owns the GUCs.
        _mark_gucs_set(scope.thread_marker)
        try:
            _audit_request_entered(
                request,
                scope.tenant_id,
                admin=scope.tenant_id is None,
                user_id=scope.user_id,
            )
            response = await self._aget_response(request)
            _vary_on_tenant_header(request, response)
            return response
        finally:
            try:
                setattr(request, "_rls_context_gucs_token", None)  # noqa: B010  -- dynamic attr on HttpRequest
                _end_context_gucs(gucs_token)
                reset_current_tenant_id(tenant_token)
                reset_rls_context_active(active_token)
            finally:
                try:
                    # False when process_exception has already cleared the GUCs. Left
                    # set if the clear fails, so the request_finished safety net can
                    # retry. Runs even if a reset fails, as in _cleanup_rls_state.
                    if _were_gucs_set():
                        await sync_to_async(_clear_request_gucs, thread_sensitive=True)(conf)
                        _clear_gucs_set_flag()
                finally:
                    # A no-op when process_exception has already ended the context.
                    _audit_request_exited(request)

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
            ``None`` for an unauthenticated request, and for a
            ``MultiTenantUser`` with neither a selection nor a default tenant
            (nothing was set), otherwise the auto-scope tenant ID (``None``
            for an admin user), the GUC values set (with ``USE_LOCAL_SET=True``,
            the values the GUC affinity guard applies), and the marker of the
            thread that set them.

        Raises:
            PermissionDenied: If a ``MultiTenantUser``'s request selects a
                tenant that is not one of theirs (nothing was set).
        """
        if not _is_authenticated(request):
            return None
        user: Any = request.user
        member = _member_of(request, user)
        if member is not None and member.tenant_id is None:
            _log_member_without_tenant(user)
            return None
        try:
            guc_vars = _resolve_request_user(user, conf, member)
            self._set_gucs_on_all_databases(guc_vars, conf)
            tenant_id = _scope_tenant_id(user, member)
        except Exception:
            logger.exception("Failed to set RLS GUC variables, clearing to prevent leak")
            _clear_gucs_on_all_databases(conf)
            raise
        logger.debug(
            "Middleware set GUCs on databases=%s for user=%s (admin=%s, tenant=%s)",
            conf.DATABASES,
            type(user).__name__,
            user.is_tenant_admin,
            tenant_id,
        )
        return _RequestScope(
            tenant_id,
            _context_gucs_of(guc_vars, conf),
            _current_thread_marker(),
            audit.user_id_of(user),
        )

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
        (see ``_reset_or_clear``); asgiref then hands the cleared tenant and
        strict-mode values back to the request by value, so the values the
        request found are put back afterwards, for a request served inside a
        live context (a test's block). A cancelled request skips Django's
        response and exception hooks, so the ``finally`` block cleans it up
        instead. Cancelled while ``process_request`` is still writing the GUCs
        in its hop, the request never learns of them (asgiref copies the hop's
        ``ContextVar`` values at the cancellation, before the hop marks them
        set), so the GUCs are cleared in a hop queued behind that one, as
        ``_aenter_request`` does. With ``USE_LOCAL_SET=True`` no transaction
        is opened here either, and ``process_request`` writes no GUC (see
        ``__acall__``).
        """
        tenant_id = get_current_tenant_id()
        active = get_rls_context_active()
        # process_request's values replace the holder; a cancelled call's block
        # cannot. Held until the cleanup below has run.
        with _holder_for_the_request(request):
            try:
                return await super().__acall__(request)
            except asyncio.CancelledError:
                if not _were_gucs_set():
                    await sync_to_async(_clear_gucs_on_open_databases, thread_sensitive=True)(
                        rls_tenants_config
                    )
                raise
            finally:
                if getattr(request, "_rls_state_reset", False) is not True or _were_gucs_set():
                    await sync_to_async(self._cleanup_rls_state, thread_sensitive=True)(request)
                if get_current_tenant_id() != tenant_id:
                    set_current_tenant_id(tenant_id)
                if get_rls_context_active() != active:
                    set_rls_context_active(active)

    async def _aget_response(self, request: HttpRequest) -> HttpResponseBase:
        """Await the downstream chain; ``async_mode`` guarantees it is a coroutine function."""
        return await cast("Awaitable[HttpResponseBase]", self.get_response(request))

    def process_request(self, request: HttpRequest) -> None:
        """Set GUC variables on all configured databases.

        Iterates over ``RLS_TENANTS["DATABASES"]`` and sets GUCs on each
        alias. If setting GUCs on one database fails, clears all GUCs on
        databases that were already set, then re-raises. Used on the
        synchronous path only; ``__acall__`` uses ``_enter_request_gucs``.

        For a ``MultiTenantUser`` who is not a tenant admin the tenant is the
        one the request selects (``RLS_TENANTS["TENANT_HEADER"]``, then the
        session), else ``rls_tenant_id``; with neither, no context is set.

        Raises:
            PermissionDenied: If a ``MultiTenantUser``'s request selects a
                tenant that is not one of theirs (nothing was set).
        """
        if not _is_authenticated(request):
            return

        conf = rls_tenants_config
        user: Any = request.user
        member = _member_of(request, user)
        if member is not None and member.tenant_id is None:
            _log_member_without_tenant(user)
            return

        # Track tokens so the except branch can reset via token (not
        # push a new value) when the ContextVar was already set before
        # the failure.
        tenant_token: Token[int | str | None] | None = None
        active_token: Token[bool] | None = None
        gucs_token: _ContextGucsToken | None = None
        try:
            guc_vars = _resolve_request_user(user, conf, member)
            self._set_gucs_on_all_databases(guc_vars, conf)
            tenant_id = _scope_tenant_id(user, member)
            logger.debug(
                "Middleware set GUCs on databases=%s for user=%s (admin=%s, tenant=%s)",
                conf.DATABASES,
                type(user).__name__,
                user.is_tenant_admin,
                tenant_id,
            )

            # Set auto-scope state for RLSManager.get_queryset().
            tenant_token = set_current_tenant_id(tenant_id)
            setattr(request, "_rls_tenant_token", tenant_token)  # noqa: B010  -- dynamic attr on HttpRequest

            # Mark RLS context as active for strict mode. This
            # distinguishes "middleware set context" from "no context".
            active_token = set_rls_context_active(True)
            setattr(request, "_rls_context_active_token", active_token)  # noqa: B010  -- dynamic attr on HttpRequest

            # The values the GUC affinity guard re-applies on other connections.
            # Not a block's holder: under ASGI, MiddlewareMixin runs this hook in a
            # sync_to_async hop of its own, which must hand the values back.
            gucs_token = _set_context_gucs(_context_gucs_of(guc_vars, conf), block=False)
            setattr(request, "_rls_context_gucs_token", gucs_token)  # noqa: B010  -- dynamic attr on HttpRequest
            _mark_gucs_set()
            _audit_request_entered(
                request,
                tenant_id,
                admin=bool(user.is_tenant_admin),
                user_id=audit.user_id_of(user),
            )
        except Exception:
            logger.exception("Failed to set RLS GUC variables, clearing to prevent leak")
            # Reset via token when available to avoid stacking unreset
            # ContextVar values; _reset_or_clear falls back to clearing when
            # the token was never created (error occurred before set call).
            _end_context_gucs(gucs_token)
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
        """Clear GUC variables and auto-scope state to prevent cross-request leaks.

        A ``MultiTenantUser``'s response also gets the tenant header in ``Vary``.
        """
        self._cleanup_rls_state(request)
        _vary_on_tenant_header(request, response)
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

        Either way, a context block the view left open (a generator suspended
        inside one) is ended, so the error response does not run in it. With
        a middleware-owned transaction the request's own context stays in
        force while the error response is built (the rollback is deferred, so
        error handlers can still query); without one the response runs with
        no context (fail-closed).
        """
        try:
            if getattr(request, "_rls_owns_transaction", False) is True:
                setattr(request, "_rls_rollback_transaction", True)  # noqa: B010 -- dynamic request marker
            else:
                self._cleanup_rls_state(request)
        finally:
            _end_blocks_left_open(request)

    @staticmethod
    def _set_gucs_on_all_databases(guc_vars: dict[str, str], conf: Any) -> None:
        """Set GUC variables on all configured database aliases.

        One ``set_config()`` statement per alias. If setting GUCs on one
        database fails, clears all GUCs on databases that were already set,
        then re-raises. With ``USE_LOCAL_SET=True`` nothing is written, in the
        request transaction of the sync path too: the GUC affinity guard
        applies the request's values transaction-locally before the first
        statement that needs them (see ``context._writes_gucs()``).

        Args:
            guc_vars: Mapping of GUC names to values.
            conf: ``RLSTenantsConfig`` instance.
        """
        if conf.USE_LOCAL_SET:
            return
        completed_aliases: list[str] = []
        gucs = _context_gucs_of(guc_vars, conf)
        try:
            for db_alias in conf.DATABASES:
                write_gucs(connections[db_alias], gucs)
                completed_aliases.append(db_alias)
        except Exception:
            # Rollback GUCs on databases that were already set.
            for done_alias in completed_aliases:
                with suppress(Exception):  # best-effort cleanup
                    _clear_alias_gucs(done_alias)
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
                entered = getattr(request, "_rls_context_gucs_token", None)
                tenant_token = getattr(request, "_rls_tenant_token", None)
                active_token = getattr(request, "_rls_context_active_token", None)
                # A request that set nothing (an anonymous one) leaves the live
                # context it is served in alone (a test's block). Otherwise what
                # has no token is cleared: values a view left behind, or the
                # tenant and flag the native path keeps tokens for itself.
                set_any = isinstance(entered, _ContextGucsToken) or any(
                    isinstance(token, Token) for token in (tenant_token, active_token)
                )
                if set_any or not _served_inside_a_context(request):
                    _end_context_gucs(entered)
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
                setattr(request, "_rls_state_reset", True)  # noqa: B010 -- request cleanup marker
        finally:
            try:
                if _were_gucs_set():
                    _clear_request_gucs(rls_tenants_config)
                    _clear_gucs_set_flag()
            finally:
                _audit_request_exited(request)
