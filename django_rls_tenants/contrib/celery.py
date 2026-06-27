"""Native Celery integration: propagate the RLS context into background tasks.

A Celery task runs outside the request/response cycle, so
:class:`~django_rls_tenants.tenants.middleware.RLSTenantMiddleware` never sets a
context for it. Without help, every task body has to remember to wrap itself in
``tenant_context()`` -- and a task that forgets silently reads zero rows
(fail-closed) or, with ``STRICT_MODE``, raises. That is a data-leak risk waiting
to happen.

This module closes the gap. The active tenant (or admin) context is captured
into the task's message **headers** when it is enqueued and restored on the
worker before the task body runs:

- :func:`rls_task` -- a drop-in replacement for ``shared_task`` that wires the
  capture/restore in for you (the recommended API).
- :class:`RLSTask` -- the Celery ``Task`` base class doing the work; use it
  directly via ``shared_task(base=RLSTask)`` when you need a custom base.
- :func:`install` / :func:`uninstall` -- an opt-in, signal-based escape hatch
  that propagates context for tasks you cannot re-base onto :class:`RLSTask`
  (for example third-party tasks).

Capture covers chains, groups, and chords: when the worker enqueues the next
step of a canvas, the upstream task is still the *current* task, so its headers
are inherited even though its ``tenant_context`` has already closed. Every task
participating in a canvas must use :class:`RLSTask` (or :func:`install`) for the
context to flow all the way through.

Scope:
    Synchronous task bodies only. ``async def`` task bodies are not supported in
    v1.3.0 -- the context is set on the calling thread, which an event loop does
    not propagate to coroutines. Keep RLS-touching task bodies synchronous.

This is an **optional** integration. It imports :mod:`celery`, which is not a
dependency of the core library; install it with ``pip install
django-rls-tenants[celery]``. Nothing in ``tenants/`` or ``rls/`` imports this
module, and it is intentionally **not** re-exported from the top-level package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

try:
    from celery import Task, current_task, shared_task
    from celery.signals import before_task_publish, task_postrun, task_prerun
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    msg = (
        "django_rls_tenants.contrib.celery requires Celery. "
        "Install it with: pip install django-rls-tenants[celery]"
    )
    raise ImportError(msg) from exc

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.errors import HINT_NO_CONTEXT
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

logger = logging.getLogger("django_rls_tenants")

# Message-header keys carrying the captured context. They travel in the task's
# protocol-v2 headers and surface on the worker as ``self.request.headers``.
_TENANT_HEADER = "rls_tenant_id"
_ADMIN_HEADER = "rls_admin"


def _capture() -> dict[str, Any]:
    """Capture the active RLS context as task-header values.

    Resolution order:

    1. A live ``tenant_context()`` -- the common case, enqueuing from a view,
       the middleware's request context, or an explicit ``tenant_context()``.
    2. A live ``admin_context()`` (active flag set but no tenant id).
    3. The currently-executing task's headers. On a worker, the next step of a
       canvas is enqueued *after* the upstream task's ``tenant_context`` has
       closed, but while that task is still :data:`celery.current_task`, so its
       headers are inherited and the context flows through the chain.

    Returns:
        A header dict to merge into the outgoing task, or an empty dict when no
        context is active (the task is enqueued unscoped -- fail-closed).
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        return {_TENANT_HEADER: tenant_id, _ADMIN_HEADER: False}
    if get_rls_context_active():
        return {_TENANT_HEADER: None, _ADMIN_HEADER: True}

    # Canvas propagation. ``current_task`` outside a task is a proxy wrapping
    # ``None`` whose attribute access raises -- ``getattr(..., None)`` turns that
    # into a safe ``None`` rather than an exception.
    request = getattr(current_task, "request", None)
    headers = getattr(request, "headers", None)
    if isinstance(headers, dict) and _TENANT_HEADER in headers:
        return {
            _TENANT_HEADER: headers[_TENANT_HEADER],
            _ADMIN_HEADER: bool(headers.get(_ADMIN_HEADER, False)),
        }
    return {}


def _request_context(request: Any) -> tuple[Any, bool]:
    """Extract ``(tenant_id, is_admin)`` from a task request's headers.

    Reads the protocol-v2 ``request.headers`` dict first (where custom headers
    land in Celery 5), then falls back to top-level request attributes for
    transports/configurations that surface headers that way instead.

    Args:
        request: The task's ``self.request`` (a Celery ``Context``), or ``None``.

    Returns:
        ``(tenant_id, is_admin)``. ``tenant_id`` is ``None`` for admin context or
        when no context was propagated; ``is_admin`` is then the discriminator.
    """
    headers = getattr(request, "headers", None)
    if isinstance(headers, dict) and _TENANT_HEADER in headers:
        return headers[_TENANT_HEADER], bool(headers.get(_ADMIN_HEADER, False))
    return getattr(request, _TENANT_HEADER, None), bool(getattr(request, _ADMIN_HEADER, False))


def _merge_headers(options: dict[str, Any]) -> dict[str, Any]:
    """Pop ``headers`` from ``options`` and merge the captured context into it.

    ``setdefault`` keeps the call idempotent and never clobbers headers a caller
    set explicitly -- an explicit ``rls_tenant_id`` header always wins.

    Args:
        options: The keyword options passed to ``apply``/``apply_async``;
            mutated in place to remove ``headers`` (re-supplied by the caller).

    Returns:
        The merged headers dict to forward as ``headers=``.
    """
    headers = dict(options.pop("headers", None) or {})
    for key, value in _capture().items():
        headers.setdefault(key, value)
    return headers


class RLSTask(Task):
    """Celery task base that carries the RLS context from caller to worker.

    On enqueue (``apply_async`` on a real broker, or ``apply`` in eager mode and
    for canvas steps) the active tenant/admin context is captured into the task
    headers. On the worker, :meth:`__call__` reads those headers and runs the
    body inside the matching ``tenant_context()`` / ``admin_context()``, which
    restores cleanly on both success and exception.

    Prefer the :func:`rls_task` decorator; subclass or pass
    ``shared_task(base=RLSTask)`` only when you need a custom base.

    Attributes:
        rls_require_context: When ``True``, a task that arrives without any
            propagated context raises :class:`NoTenantContextError` instead of
            running unscoped. Defaults to ``False`` (fail-closed: run with no
            context, so RLS returns zero rows). Set it per task for jobs that
            must never run tenant-blind.
    """

    abstract = True
    rls_require_context: bool = False

    def apply_async(self, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        """Capture the active context into headers, then enqueue normally."""
        merged_headers = _merge_headers(options)  # also pops "headers" from options
        return super().apply_async(args, kwargs, headers=merged_headers, **options)

    def apply(self, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        """Capture context for eager execution and for canvas steps.

        Eager mode and canvas (chain/group) dispatch call ``apply`` directly
        rather than going through ``apply_async``, so the capture is wired in
        here as well to cover those paths.
        """
        merged_headers = _merge_headers(options)  # also pops "headers" from options
        return super().apply(args, kwargs, headers=merged_headers, **options)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Restore the propagated RLS context around the task body."""
        tenant_id, is_admin = _request_context(self.request)
        if tenant_id is not None:
            with tenant_context(tenant_id):
                return super().__call__(*args, **kwargs)
        if is_admin:
            with admin_context():
                return super().__call__(*args, **kwargs)
        if self.rls_require_context:
            msg = f"Task {self.name!r} ran without an RLS context."
            raise NoTenantContextError(msg, hint=HINT_NO_CONTEXT)
        return super().__call__(*args, **kwargs)


def rls_task(*args: Any, **options: Any) -> Any:
    """Define a Celery task that propagates the RLS context. Like ``shared_task``.

    A thin wrapper over :func:`celery.shared_task` that defaults ``base`` to
    :class:`RLSTask`. Use it exactly as you would ``shared_task`` -- bare or with
    options::

        from django_rls_tenants.contrib.celery import rls_task

        @rls_task
        def reindex(): ...

        @rls_task(bind=True, max_retries=3)
        def sync(self): ...

    Enqueue inside an RLS context and the worker runs the body scoped to the same
    tenant::

        with tenant_context(tenant.pk):
            reindex.delay()          # runs on the worker under tenant_context(tenant.pk)

    Args:
        *args: Forwarded to ``shared_task`` (e.g. the decorated function).
        **options: Forwarded to ``shared_task``. ``base`` defaults to
            :class:`RLSTask`; pass your own ``base`` (a ``RLSTask`` subclass) to
            override -- for example to set ``rls_require_context = True``.

    Returns:
        The shared task, or a decorator when called with options.
    """
    options.setdefault("base", RLSTask)
    return shared_task(*args, **options)


# ---------------------------------------------------------------------------
# Signal-based escape hatch (opt-in via install())
# ---------------------------------------------------------------------------

_DISPATCH_UID = "django_rls_tenants.contrib.celery"

# Active context managers entered by the prerun signal, keyed by task id. A list
# (stack) tolerates a task id re-entering before its first run unwinds; postrun
# pops the matching entry. Only populated when install() is active.
_signal_contexts: dict[str, list[AbstractContextManager[None]]] = {}


def _before_task_publish(
    headers: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """Inject the active RLS context into a task's outgoing headers (publish side).

    Connected by :func:`install`. Mirrors :meth:`RLSTask.apply_async` for tasks
    that are not :class:`RLSTask` instances. Protocol v1 has no ``headers``
    mapping, so there is nothing to do then.
    """
    if headers is None:
        return
    for key, value in _capture().items():
        headers.setdefault(key, value)


def _safe_exit(context: AbstractContextManager[None]) -> None:
    """Exit a propagated context, logging instead of raising on teardown failure.

    The signal-path teardown runs inside Celery's signal dispatcher, so a
    GUC-restore error (for example a dropped connection) must not escape into
    it. The context managers reset their ``ContextVar`` state *before* the GUC
    statement that might fail, so swallowing here never strands the in-process
    tenant id -- at worst a stale GUC remains on a connection that is about to
    be recycled.
    """
    try:
        context.__exit__(None, None, None)
    except Exception:
        logger.exception("django-rls-tenants: failed to exit propagated RLS context")


def _task_prerun(
    task_id: str | None = None,
    task: Any = None,
    **_: Any,
) -> None:
    """Enter the propagated RLS context before a task body runs (worker side).

    Connected by :func:`install`. :class:`RLSTask` instances manage their own
    context in :meth:`RLSTask.__call__`, so they are skipped here to avoid
    entering it twice.
    """
    if task_id is None or isinstance(task, RLSTask):
        return
    tenant_id, is_admin = _request_context(getattr(task, "request", None))
    if tenant_id is not None:
        context: AbstractContextManager[None] = tenant_context(tenant_id)
    elif is_admin:
        context = admin_context()
    else:
        return
    try:
        context.__enter__()
    except Exception:
        logger.exception(
            "django-rls-tenants: failed to enter propagated RLS context for task %s; "
            "it runs without a tenant context (fail-closed)",
            task_id,
        )
        return
    _signal_contexts.setdefault(task_id, []).append(context)


def _task_postrun(task_id: str | None = None, **_: Any) -> None:
    """Exit the context entered by :func:`_task_prerun` (worker side)."""
    if task_id is None:
        return
    stack = _signal_contexts.get(task_id)
    if not stack:
        return
    context = stack.pop()
    if not stack:
        del _signal_contexts[task_id]
    _safe_exit(context)


def install() -> None:
    """Globally propagate RLS context for *all* Celery tasks, via signals.

    Connects ``before_task_publish`` (capture into headers) and
    ``task_prerun`` / ``task_postrun`` (restore around the body) so context flows
    even for tasks that are not based on :class:`RLSTask`. Use it as an escape
    hatch for third-party or legacy tasks you cannot re-base.

    Prefer :func:`rls_task` / :class:`RLSTask` where you can: they restore the
    context for the whole body including its own ``apply_async`` calls, are
    scoped per task, and need no global wiring. ``install()`` and the base class
    compose safely -- ``RLSTask`` instances are skipped by the signal handlers.

    Call it once during startup (for example in your Celery app module). It is
    idempotent: a repeated call does not double-connect. Reverse it with
    :func:`uninstall`.
    """
    before_task_publish.connect(_before_task_publish, dispatch_uid=_DISPATCH_UID, weak=False)
    task_prerun.connect(_task_prerun, dispatch_uid=_DISPATCH_UID, weak=False)
    task_postrun.connect(_task_postrun, dispatch_uid=_DISPATCH_UID, weak=False)


def uninstall() -> None:
    """Disconnect the signal handlers connected by :func:`install`.

    Idempotent: safe to call when :func:`install` was never called. Does not
    affect tasks based on :class:`RLSTask`, which never relied on the signals.
    Any contexts still open from :func:`_task_prerun` are exited here as a
    best-effort cleanup so a stale context cannot leak into the next task.

    Warning:
        Do not call this while tasks are still executing on *other* threads. A
        ``ContextVar`` token can only be reset on the thread that created it, and
        Django database connections are thread-local, so this cleanup only
        unwinds contexts entered on the calling thread -- an in-flight task on a
        worker thread keeps its context until it finishes. Call ``uninstall()``
        at shutdown, or from the worker thread between tasks.
    """
    before_task_publish.disconnect(_before_task_publish, dispatch_uid=_DISPATCH_UID)
    task_prerun.disconnect(_task_prerun, dispatch_uid=_DISPATCH_UID)
    task_postrun.disconnect(_task_postrun, dispatch_uid=_DISPATCH_UID)
    for stack in list(_signal_contexts.values()):
        for context in reversed(stack):
            _safe_exit(context)
    _signal_contexts.clear()
