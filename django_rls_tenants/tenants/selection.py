"""Tenant selection for users who belong to several tenants (#47).

A user that satisfies :class:`~django_rls_tenants.tenants.types.MultiTenantUser`
and is not a tenant admin selects the tenant a request runs in:

- per request, with the ``RLS_TENANTS["TENANT_HEADER"]`` header (default
  ``X-Tenant``), which an API client sends;
- for the session, with :func:`switch_tenant`, which stores the selection under
  ``RLS_TENANTS["TENANT_SESSION_KEY"]`` (default ``rls_tenant``) until
  :func:`clear_tenant_selection` or the end of the session. The selection
  belongs to the user the session is logged in as: a request authenticated
  otherwise (a DRF token) neither uses nor drops it.

A request that selects nothing runs in the user's ``rls_tenant_id``.
``RLSTenantMiddleware``, ``RLSTenantAPIViewMixin`` and ``RLSTenantModelAdmin``
resolve the request's tenant with :func:`_request_tenant_id`: the header
first, then the session, then ``rls_tenant_id``. A selection that is not one
of the user's tenants is refused with ``PermissionDenied`` (a 403), never
replaced by another tenant; a stale one in the session (a membership revoked
since) is removed from it as it is refused, so the next request runs in
``rls_tenant_id``. Every refusal gives the same message, whether the tenant
exists or not. A response whose tenant could depend on the header gets it in
``Vary`` (:func:`_vary_on_tenant_header`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from django.apps import apps
from django.contrib.auth import SESSION_KEY
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.cache import patch_vary_headers

from django_rls_tenants.exceptions import RLSConfigurationError
from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.types import MultiTenantUser

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase
    from django.http import HttpRequest, HttpResponseBase

logger = logging.getLogger("django_rls_tenants")

NOT_SELECTABLE_MESSAGE = "The selected tenant is not one you can access."
"""The message of every refused selection: it does not tell whether the tenant exists."""

NOT_SESSION_USER_MESSAGE = (
    "A tenant selection is stored in the session of a logged-in user; this request is "
    "authenticated otherwise. Select the tenant with the RLS_TENANTS['TENANT_HEADER'] "
    "header instead."
)
"""The message of ``switch_tenant()`` for a user the session is not logged in as."""

_SHOWN_LENGTH = 64

# Request attribute naming the tenant header a member's response depends on.
_VARY_ATTRIBUTE = "_rls_vary_on_tenant_header"

Via = Literal["header", "session", "query", "switch"]
"""Where a selection came from: the header, the session, the admin switcher's query
parameter, or a :func:`switch_tenant` call."""


def _selects_tenants(user: object) -> bool:
    """Return whether ``user`` selects a tenant per request.

    ``True`` for a :class:`~django_rls_tenants.tenants.types.MultiTenantUser`
    who is not a tenant admin (a tenant admin runs in ``admin_context()``).
    Reads neither ``rls_tenant_ids`` nor ``rls_tenant_id``.

    Args:
        user: An authenticated user.
    """
    return isinstance(user, MultiTenantUser) and not user.is_tenant_admin


def _tenant_pk(value: Any) -> Any:
    """Return ``value`` as the tenant model's primary key (``int``, ``UUID``, ...).

    Raises:
        ValidationError: If ``value`` is not a valid key of that type.
    """
    tenant_model = apps.get_model(rls_tenants_config.TENANT_MODEL)
    pk = tenant_model._meta.pk  # noqa: SLF001
    assert pk is not None  # noqa: S101  -- a concrete model always has one
    return pk.to_python(value)


def _selectable(user: MultiTenantUser) -> set[Any]:
    """Return the primary keys of the tenants ``user`` may select: ``rls_tenant_ids`` + default."""
    selectable = {_tenant_pk(tenant_id) for tenant_id in user.rls_tenant_ids}
    if user.rls_tenant_id is not None:
        selectable.add(_tenant_pk(user.rls_tenant_id))
    selectable.discard(None)
    return selectable


def _may_select(user: MultiTenantUser, value: Any) -> Any:
    """Return ``value`` as a primary key if ``user`` may select that tenant, else ``None``."""
    try:
        tenant_id = _tenant_pk(value)
    except (ValidationError, TypeError, ValueError):
        return None
    if tenant_id is None or tenant_id not in _selectable(user):
        return None
    return tenant_id


def _other_tenants(user: MultiTenantUser) -> set[Any]:
    """Return the tenants ``user`` may select besides ``rls_tenant_id``; the admin offers them."""
    selectable = _selectable(user)
    if user.rls_tenant_id is not None:
        selectable.discard(_tenant_pk(user.rls_tenant_id))
    return selectable


def _shown(raw: Any) -> Any:
    """Return ``raw`` for an audit record: a string sent by a client is cut to 64 characters."""
    if isinstance(raw, str) and len(raw) > _SHOWN_LENGTH:
        return f"{raw[:_SHOWN_LENGTH]}..."
    return raw


def _refuse(
    user: object,
    raw: Any,
    *,
    source: audit.Source,
    via: Via,
    message: str = NOT_SELECTABLE_MESSAGE,
) -> PermissionDenied:
    """Log a ``context.denied`` record for a refused selection; return the error to raise."""
    audit.denied(
        source,
        "tenant_not_selectable",
        user_id=audit.user_id_of(user),
        selected=_shown(raw),
        via=via,
    )
    return PermissionDenied(message)


def _session_of(request: HttpRequest) -> SessionBase | None:
    """Return the request's session, ``None`` without ``SessionMiddleware``."""
    return getattr(request, "session", None)


def _is_session_of(session: SessionBase, user: Any) -> bool:
    """Return whether ``session`` is logged in as ``user`` (Django's ``login()`` stored its pk).

    A selection in the session belongs to the session's user: a request that
    DRF authenticated with a token of another user, say, must neither use it
    nor drop it.
    """
    stored = session.get(SESSION_KEY)
    if stored is None:
        return False
    meta = getattr(user, "_meta", None)
    if meta is not None:
        return bool(stored == meta.pk.value_to_string(user))
    pk = getattr(user, "pk", None)
    return pk is not None and stored == str(pk)


def _depends_on_header(request: HttpRequest, header: str) -> None:
    """Record that the response depends on the tenant header (:func:`_vary_on_tenant_header`)."""
    # On the Django request, also when ``request`` is DRF's, whose attributes
    # the middleware does not see.
    setattr(getattr(request, "_request", request), _VARY_ATTRIBUTE, header)


def _vary_on_tenant_header(request: HttpRequest, response: HttpResponseBase) -> None:
    """Add the tenant header to ``Vary`` when the request's tenant could depend on it.

    A cache that keys a response on the URL and the session or token alone
    would otherwise give a member's request for one of their tenants the
    response cached for another.
    """
    header = getattr(request, _VARY_ATTRIBUTE, None)
    if isinstance(header, str):
        patch_vary_headers(response, (header,))


def _request_tenant_id(request: HttpRequest, user: Any, *, source: audit.Source) -> Any:
    """Return the tenant the request of a user who selects tenants runs in.

    The header's tenant if the request sends ``RLS_TENANTS["TENANT_HEADER"]``,
    else the session's selection (when the session is logged in as ``user``),
    else ``user.rls_tenant_id`` (``None`` if the user has no default either).
    Records that the response depends on the header.

    Args:
        request: The Django (or DRF) request.
        user: A user for whom :func:`_selects_tenants` is ``True``.
        source: The integration resolving it, for the audit record of a refusal.

    Raises:
        PermissionDenied: If the request selects a tenant that is not one of
            the user's (a malformed or unknown value included). A refused
            session selection is removed from the session.
    """
    conf = rls_tenants_config
    header = conf.TENANT_HEADER
    if header is not None:
        _depends_on_header(request, header)
        raw = request.headers.get(header)
        if raw is not None:
            tenant_id = _may_select(user, raw)
            if tenant_id is None:
                raise _refuse(user, raw, source=source, via="header")
            return tenant_id
    key = conf.TENANT_SESSION_KEY
    session = _session_of(request)
    if key is not None and session is not None:
        raw = session.get(key)
        if raw is not None and _is_session_of(session, user):
            tenant_id = _may_select(user, raw)
            if tenant_id is None:
                session.pop(key, None)
                raise _refuse(user, raw, source=source, via="session")
            return tenant_id
    return user.rls_tenant_id


def _selection_session(request: HttpRequest) -> tuple[SessionBase, str]:
    """Return the request's session and the key a selection is stored under.

    Raises:
        RLSConfigurationError: If ``RLS_TENANTS["TENANT_SESSION_KEY"]`` is
            ``None`` or the request has no session.
    """
    key = rls_tenants_config.TENANT_SESSION_KEY
    if key is None:
        msg = (
            "Tenant selection in the session is turned off (RLS_TENANTS['TENANT_SESSION_KEY'] "
            "is None). Set it to a session key, e.g. 'rls_tenant', or select the tenant with "
            "the RLS_TENANTS['TENANT_HEADER'] header."
        )
        raise RLSConfigurationError(msg)
    session = _session_of(request)
    if session is None:
        msg = (
            "The request has no session. Add "
            "'django.contrib.sessions.middleware.SessionMiddleware' to MIDDLEWARE to store "
            "a tenant selection."
        )
        raise RLSConfigurationError(msg)
    return session, key


def _switch(request: HttpRequest, value: Any, *, source: audit.Source, via: Via) -> Any:
    """Store a tenant selection of ``request.user`` in the session; see :func:`switch_tenant`."""
    session, key = _selection_session(request)
    user: Any = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not _selects_tenants(user):
        raise _refuse(user, value, source=source, via=via)
    if not _is_session_of(session, user):
        # A user DRF authenticated otherwise (a token): the selection would
        # never apply to them, and must not replace the session user's.
        raise _refuse(user, value, source=source, via=via, message=NOT_SESSION_USER_MESSAGE)
    tenant_id = _may_select(user, value)
    if tenant_id is None:
        raise _refuse(user, value, source=source, via=via)
    previous = session.get(key)
    # The session serializer takes strings; a UUID is not JSON.
    session[key] = str(tenant_id)
    audit.emit(
        audit.TENANT_SWITCHED,
        source=source,
        tenant_id=tenant_id,
        admin=False,
        user_id=audit.user_id_of(user),
        previous=previous,
    )
    logger.debug("Tenant selection of user=%s set to tenant=%s", audit.user_id_of(user), tenant_id)
    return tenant_id


def _clear(request: HttpRequest, *, source: audit.Source) -> None:
    """Remove the session's tenant selection; see :func:`clear_tenant_selection`."""
    session, key = _selection_session(request)
    user: Any = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not _is_session_of(session, user):
        return  # nothing of this user's to clear
    previous = session.pop(key, None)
    if previous is None:
        return
    audit.emit(
        audit.TENANT_SWITCHED,
        source=source,
        admin=False,
        user_id=audit.user_id_of(user),
        previous=previous,
    )


def switch_tenant(request: HttpRequest, tenant_id: Any) -> Any:
    """Select the tenant the user's next requests run in, for the rest of the session.

    Call it from a view that handles the user's choice (a ``POST``, so that
    Django's CSRF protection covers it), then redirect: the request that
    calls it keeps the context it started in. ``RLSTenantMiddleware``,
    ``RLSTenantAPIViewMixin`` and ``RLSTenantModelAdmin`` run the following
    requests of the session in ``tenant_id``, unless one sends the
    ``RLS_TENANTS["TENANT_HEADER"]`` header, until :func:`clear_tenant_selection`,
    another ``switch_tenant()`` or the end of the session (``logout()``
    flushes it). Each request checks the selection again, so a membership
    revoked meanwhile ends it with a 403.

    Logs a ``tenant.switched`` audit record.

    Args:
        request: The request; ``request.user`` must be a ``MultiTenantUser``
            who is not a tenant admin.
        tenant_id: The tenant to select: ``rls_tenant_id`` or one of
            ``rls_tenant_ids``, as a primary key or its string form.

    Returns:
        The tenant's primary key, converted to the tenant model's type.

    Raises:
        django.core.exceptions.PermissionDenied: If the user is anonymous, a
            tenant admin, not a ``MultiTenantUser``, not the user the session
            is logged in as (a DRF token client: it selects with the header),
            or may not select ``tenant_id``; the session is left as it was.
        RLSConfigurationError: If ``RLS_TENANTS["TENANT_SESSION_KEY"]`` is
            ``None`` or the request has no session.
    """
    return _switch(request, tenant_id, source="selection", via="switch")


def clear_tenant_selection(request: HttpRequest) -> None:
    """Remove the session's tenant selection: the next requests run in ``rls_tenant_id``.

    Logs a ``tenant.switched`` audit record (``rls_tenant_id=None``) if there
    was a selection. Does nothing unless the session is logged in as
    ``request.user``.

    Raises:
        RLSConfigurationError: If ``RLS_TENANTS["TENANT_SESSION_KEY"]`` is
            ``None`` or the request has no session.
    """
    _clear(request, source="selection")
