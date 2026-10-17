"""Audit logging: structured records of RLS context changes and refusals (#48).

Every record goes to the ``django_rls_tenants.audit`` logger (a child of
``django_rls_tenants``), so Django's ``LOGGING`` setting turns the trail on and
routes it; there is no library setting. Each record carries its fields as
``LogRecord`` attributes (``extra=``) prefixed with ``rls_``, which JSON
formatters emit without parsing the message:

- ``rls_event``: ``context.entered`` / ``context.exited`` (``INFO``), and
  ``context.denied``, ``fk.cross_tenant_rejected``, ``gucs.cleared``
  (``WARNING``).
- ``rls_source``: the code path that set, ended or refused the context
  (:data:`Source`).
- ``rls_tenant_id``, ``rls_admin``, ``rls_user_id``: the context and the
  user's primary key, ``None`` where unknown. No other user data is logged.
- Per event: ``rls_using`` (the alias a context block was entered on),
  ``rls_reason`` (:data:`Reason`), ``rls_model`` (a model label),
  ``rls_fields`` (foreign key names), ``rls_databases`` (aliases).

``for_user()`` scopes emit nothing (they run once per SQL statement); nor do
the context blocks the library enters for its own lookups (the tenant of a
foreign key's target, the tenants a management command lists).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from contextlib import AbstractAsyncContextManager, AbstractContextManager

AUDIT_LOGGER_NAME = "django_rls_tenants.audit"
"""Name of the logger the audit records go to; configure it in Django's ``LOGGING``."""

audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)

CONTEXT_ENTERED = "context.entered"
CONTEXT_EXITED = "context.exited"
CONTEXT_DENIED = "context.denied"
FK_CROSS_TENANT_REJECTED = "fk.cross_tenant_rejected"
GUCS_CLEARED = "gucs.cleared"

Source = Literal[
    "admin",
    "celery",
    "command",
    "context_manager",
    "decorator",
    "drf",
    "for_user",
    "middleware",
    "queryset",
    "safety_net",
    "validation",
]
"""Where a record comes from: an integration, a context manager, or a check."""

Reason = Literal[
    "context_mismatch",
    "no_context",
    "object_of_other_tenant",
    "strict_mode",
    "tenant_id_none",
    "user_without_tenant",
]
"""Why a ``context.denied`` record refused the context."""


class Origin(NamedTuple):
    """Who enters a context block: its source (``None``: not audited) and user's pk."""

    source: Source | None
    user_id: Any


# A block entered directly by the code: tenant_context() / admin_context().
_DIRECT = Origin("context_manager", None)

# Read by the context managers when they are entered, so an integration can
# attribute the block it enters (see attributed()). Reset as soon as the block
# is entered: blocks nested inside it are the code's own.
_origin: ContextVar[Origin] = ContextVar("rls_audit_origin", default=_DIRECT)


def user_id_of(user: object) -> Any:
    """Return the primary key an audit record identifies ``user`` by, or ``None``."""
    return getattr(user, "pk", None)


def emit(
    event: str,
    *,
    source: Source,
    level: int = logging.INFO,
    tenant_id: Any = None,
    admin: bool | None = None,
    user_id: Any = None,
    **details: Any,
) -> None:
    """Log one audit record, unless the audit logger drops ``level``.

    The message lists the fields that are not ``None``, for a plain-text
    handler: ``context.entered source=middleware tenant_id=42 admin=False
    user_id=7``.

    Args:
        event: The event name (``CONTEXT_ENTERED``, ...).
        source: Where the record comes from.
        level: ``logging.INFO`` for a context change, ``logging.WARNING``
            for a refusal or a cleanup that should not have been needed.
        tenant_id: The tenant of the context, ``None`` for none or an admin.
        admin: Whether the context is the admin bypass, ``None`` if unknown.
        user_id: The user's primary key, ``None`` if unknown.
        **details: Event-specific fields (``using``, ``reason``, ...),
            logged as ``rls_<name>``.
    """
    if not audit_logger.isEnabledFor(level):
        return
    fields: dict[str, Any] = {
        "source": source,
        "tenant_id": tenant_id,
        "admin": admin,
        "user_id": user_id,
        **details,
    }
    extra = {"rls_event": event, **{f"rls_{name}": value for name, value in fields.items()}}
    text = " ".join(f"{name}={value}" for name, value in fields.items() if value is not None)
    audit_logger.log(level, "%s %s", event, text, extra=extra)


def denied(
    source: Source,
    reason: Reason,
    *,
    user_id: Any = None,
    tenant_id: Any = None,
    admin: bool | None = None,
    **details: Any,
) -> None:
    """Log a ``context.denied`` record (``WARNING``).

    Args:
        source: The code path that refused.
        reason: Why it refused.
        user_id: The primary key of the user refused (:func:`user_id_of`), if any.
        tenant_id: The tenant of the active context, if any.
        admin: Whether the active context is the admin bypass, if known.
        **details: Further fields (``model``).
    """
    emit(
        CONTEXT_DENIED,
        source=source,
        level=logging.WARNING,
        tenant_id=tenant_id,
        admin=admin,
        user_id=user_id,
        reason=reason,
        **details,
    )


def current_origin() -> Origin:
    """Return the origin a context block entered now is attributed to."""
    return _origin.get()


def context_event(event: str, origin: Origin, *, tenant_id: Any, using: str) -> None:
    """Log ``context.entered`` / ``context.exited`` for a context block, unless it is silenced.

    Args:
        event: ``CONTEXT_ENTERED`` or ``CONTEXT_EXITED``.
        origin: What :func:`current_origin` returned when the block was entered.
        tenant_id: The block's tenant, ``None`` for ``admin_context()``.
        using: The alias the block was entered on.
    """
    if origin.source is None:
        return
    emit(
        event,
        source=origin.source,
        tenant_id=tenant_id,
        admin=tenant_id is None,
        user_id=origin.user_id,
        using=using,
    )


@contextmanager
def attributed(
    context: AbstractContextManager[None],
    *,
    source: Source | None,
    user: object = None,
) -> Iterator[None]:
    """Enter ``context`` with its audit records attributed to ``source`` and ``user``.

    The context managers read the attribution when they are entered, so the
    blocks the code inside ``context`` enters are attributed to themselves.

    Args:
        context: A context block (``tenant_context()``, ``admin_context()``,
            or anything that enters one).
        source: The integration that enters it; ``None`` silences it (a
            lookup the library makes for itself).
        user: The user the block is entered for, if any.
    """
    with ExitStack() as stack:
        token = _origin.set(Origin(source, user_id_of(user)))
        try:
            stack.enter_context(context)
        finally:
            _origin.reset(token)
        yield


@asynccontextmanager
async def async_attributed(
    context: AbstractAsyncContextManager[None],
    *,
    source: Source | None,
    user: object = None,
) -> AsyncIterator[None]:
    """Async counterpart of :func:`attributed`, for ``async with``."""
    async with AsyncExitStack() as stack:
        token = _origin.set(Origin(source, user_id_of(user)))
        try:
            await stack.enter_async_context(context)
        finally:
            _origin.reset(token)
        yield


def silenced(context: AbstractContextManager[None]) -> AbstractContextManager[None]:
    """Enter ``context`` without audit records: a lookup the library makes for itself."""
    return attributed(context, source=None)
