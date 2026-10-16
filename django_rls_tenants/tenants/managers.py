"""Tenant-aware manager and queryset.

``TenantQuerySet`` solves the lazy evaluation problem: ``for_user()``
stores the user, and every method that executes SQL applies that user's
RLS context (the GUC variables and the tenant state ``ContextVar``
values) around the statements it runs, not when the queryset is created.
See ``TenantQuerySet._rls_scope()``.

Auto-scope support: when a tenant context is active (via
``tenant_context()``, ``admin_context()``, or ``RLSTenantMiddleware``),
the queryset automatically adds ``WHERE tenant_id = X`` to every query.
For joins via ``select_related()``, tenant filters are also propagated
to joined RLS-protected tables, enabling PostgreSQL to use composite
indexes on both sides of the join.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager, nullcontext
from functools import lru_cache
from itertools import islice
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.core.exceptions import FieldDoesNotExist
from django.db import connections, models, router, transaction
from django.db.models import Q

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import set_guc
from django_rls_tenants.tenants.affinity import writing
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import (
    _context_gucs_of,
    _resolve_user_guc_vars,
    _restore_tenant_gucs,
    _rls_state,
    _session_of,
    _snapshot_gucs,
    _writes_gucs,
)
from django_rls_tenants.tenants.errors import HINT_NO_CONTEXT
from django_rls_tenants.tenants.state import (
    _current_live,
    get_current_tenant_id,
    get_rls_context_active,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping

    from django_rls_tenants.tenants.types import TenantUser

try:
    # django-stubs 6.1.2 still declares the class under its pre-rename name,
    # RemovedInDjango70Warning (a runtime alias of this one).
    from django.utils.deprecation import (  # type: ignore[attr-defined]
        RemovedInDjango2028Warning,
        warn_about_external_use,
    )
except ImportError:  # Django < 6.1: select_related() with no arguments is not deprecated.
    _SELECT_RELATED_NO_ARGS_DEPRECATED = False
else:
    _SELECT_RELATED_NO_ARGS_DEPRECATED = True

logger = logging.getLogger("django_rls_tenants")

# QuerySet.iterator()'s default chunk size, hard-coded in QuerySet._iterator(),
# and QuerySet.aiterator()'s default. TenantQuerySet._scoped_chunks() pulls the
# same number of rows per scope, so each scope covers one cursor fetch.
_ITERATOR_CHUNK_SIZE = 2000


def _next_chunk(rows: Iterator[Any], size: int) -> list[Any]:
    """Pull the next ``size`` items from ``rows``."""
    return list(islice(rows, size))


def _is_rls_protected(model: type[models.Model]) -> bool:
    """Check if a Django model has an ``RLSConstraint`` in its Meta.

    Uses lazy import and caching to avoid circular imports and repeated
    introspection.

    Args:
        model: A Django model class.

    Returns:
        ``True`` if the model has at least one ``RLSConstraint``.
    """
    return model in _rls_model_cache()


@lru_cache(maxsize=1)
def _rls_model_cache() -> frozenset[type[models.Model]]:
    """Build a cached set of all RLS-protected models.

    Called lazily after Django's app registry is ready. The result is
    cached for the lifetime of the process (model set doesn't change).

    Raises:
        RuntimeError: If called before ``django.apps`` is fully ready,
            to prevent caching an incomplete model set.
    """
    # Lazy import avoids a circular import.
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.rls.constraints import RLSConstraint  # noqa: PLC0415

    if not apps.ready:
        msg = (
            "_rls_model_cache() called before Django apps are ready. "
            "This would cache an incomplete model set. Ensure all apps "
            "are loaded before accessing RLS-protected querysets."
        )
        raise RuntimeError(msg)

    protected: set[type[models.Model]] = set()
    for model in apps.get_models():
        if any(isinstance(c, RLSConstraint) for c in model._meta.constraints):  # noqa: SLF001
            protected.add(model)
    return frozenset(protected)


# Type ignore explanation: Django's QuerySet is generic at runtime but
# unparameterised in django-stubs; suppressed to avoid false positives. The
# overrides below return Any for rows and instances for the same reason.
class TenantQuerySet(models.QuerySet):  # type: ignore[type-arg]
    """QuerySet that applies a ``for_user()`` user's RLS context when it runs SQL.

    ``for_user()`` stores the user and sets nothing. Every method that
    executes SQL then runs its statements in a scope that sets the user's
    GUC variables and tenant state on the connection the SQL uses and puts
    the previous values back afterwards (see "Lazy QuerySet Evaluation" in
    the architecture guide). That covers
    iteration (``_fetch_all()``), ``count()``, ``exists()``,
    ``aggregate()``, ``iterator()``, ``aiterator()``, ``update()``,
    ``delete()``, ``create()``, ``bulk_create()``, ``bulk_update()``,
    ``update_or_create()`` and ``explain()``; everything else (``get()``,
    ``first()``, ``get_or_create()``, ``contains()``, ``in_bulk()``, the
    ``a``-prefixed async methods, ...) runs its SQL through one of those.

    When auto-scope is active (via ``tenant_context()`` or middleware),
    ``select_related()`` automatically adds ``WHERE related.tenant_id = X``
    for joined RLS-protected tables, enabling index usage on both sides
    of the join.
    """

    _rls_user: TenantUser | None

    # Internal QuerySet attributes this class reads or pins; django-stubs
    # does not declare them.
    _db: str | None
    _hints: dict[str, Any]
    _prefetch_related_lookups: tuple[Any, ...]
    _prefetch_done: bool

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rls_user = None

    def for_user(self, as_user: TenantUser) -> TenantQuerySet:
        """Scope this queryset to the given user's tenant.

        For admin users: returns all rows (RLS admin bypass at eval time).
        For tenant users: returns rows matching the user's tenant.

        The queryset remains lazy and chainable. The user's GUC variables
        are set when the queryset runs SQL, not when ``for_user()`` is
        called, and only for the duration of that SQL: every method that
        executes a query applies them, and the values found on the
        connection before are restored afterwards (see "Lazy QuerySet
        Evaluation" in the architecture guide).

        Args:
            as_user: User object satisfying the ``TenantUser`` protocol.
        """
        qs = self._clone()
        qs._rls_user = as_user  # noqa: SLF001
        if not as_user.is_tenant_admin:
            # Defense-in-depth: Django-level filter provides isolation
            # even if GUC is misconfigured. RLS provides isolation even
            # if the Django filter is bypassed (e.g., raw SQL).
            conf = rls_tenants_config
            qs = qs.filter(**{f"{conf.TENANT_FK_FIELD}_id": as_user.rls_tenant_id})
        return qs

    def select_related(self, *fields: Any) -> TenantQuerySet:
        """Override to add tenant filters on joined RLS-protected tables.

        When a tenant context is active (or ``for_user()`` was called),
        adds ``WHERE related.tenant_id = X`` for each explicitly named
        relation that targets an RLS-protected model. This enables
        PostgreSQL to use composite indexes on joined tables instead of
        relying solely on per-row RLS ``current_setting()`` evaluation.

        Falls back to ``super().select_related()`` when no tenant scope
        is active. Called with no arguments (select-all mode) it adds no
        tenant filters; Django 6.1 deprecates that form, and the
        ``RemovedInDjango2028Warning`` names the caller's line, not this
        method.

        ``select_related(None)`` clears the selection without adding
        tenant filters. ``select_related(False)`` is treated the same way
        as a convenience kept for existing callers; Django itself has
        never accepted ``False``.
        """
        # Django never accepted False (it reaches add_select_related() and
        # fails with 'bool' object has no attribute 'split'); normalize it
        # to None, which clears the selection.
        if fields in ((False,), (None,)):
            return super().select_related(None)
        if not fields:
            if not _SELECT_RELATED_NO_ARGS_DEPRECATED:
                return super().select_related()
            # These guards (and _chain() below) mirror QuerySet.select_related();
            # all three are internal Django APIs that django-stubs does not declare.
            self._not_support_combined_queries("select_related")  # type: ignore[attr-defined]
            if self._fields is not None:  # type: ignore[attr-defined]
                raise TypeError("Cannot call select_related() after .values() or .values_list()")
            # Django 6.1 deprecates the no-argument form through
            # warn_about_external_use(), which blames the first frame outside
            # django.db.models -- this override. Emit the same warning
            # ourselves with this package skipped, so the user's call site is
            # named, and set the flag the way QuerySet.select_related() does,
            # so Django does not warn a second time. Django-internal callers
            # (the admin changelist) get no warning from us; Django deprecates
            # list_select_related=True on its own.
            # RemovedInDjango2028Warning: when Django removes the no-argument
            # form, raise TypeError here too.
            warn_about_external_use(
                "Calling select_related() with no arguments is deprecated. "
                "Specify the fields to fetch instead.",
                category=RemovedInDjango2028Warning,
                skip_name_prefixes=("django.db.models", "django_rls_tenants."),
            )
            everything: TenantQuerySet = self._chain()  # type: ignore[attr-defined]
            everything.query.select_related = True
            return everything
        tenant_id = self._get_active_tenant_id()
        qs: TenantQuerySet = super().select_related(*fields)
        if tenant_id is None:
            return qs
        conf = rls_tenants_config
        fk_field_id = f"{conf.TENANT_FK_FIELD}_id"
        for field_path in fields:
            related_model, nullable = _resolve_related_path(self.model, field_path)
            if related_model is not None and _is_rls_protected(related_model):
                tenant_filter = Q(**{f"{field_path}__{fk_field_id}": tenant_id})
                # Preserve LEFT OUTER JOIN semantics when *any* hop of the
                # path is nullable: include rows whose chain ends in NULL.
                # Without this, .filter() forces an INNER JOIN which
                # silently drops those rows. The check must cover every
                # segment, not just the first or the last one -- a nullable
                # intermediate FK breaks the chain just as well (#79).
                if nullable:
                    tenant_filter = tenant_filter | Q(**{f"{field_path}__isnull": True})
                qs = qs.filter(tenant_filter)
        return qs

    def _get_active_tenant_id(self) -> int | str | None:
        """Return the active tenant ID from ContextVar or ``_rls_user``.

        Checks the ContextVar first (set by ``tenant_context()`` / middleware),
        then falls back to ``_rls_user`` (set by ``for_user()``).
        """
        ctx_id = get_current_tenant_id()
        if ctx_id is not None:
            return ctx_id
        if self._rls_user is not None and not self._rls_user.is_tenant_admin:
            return self._rls_user.rls_tenant_id
        return None

    def _clone(self) -> TenantQuerySet:
        """Propagate ``_rls_user`` to cloned querysets."""
        # Type ignore explanation: _clone() is an internal Django API not typed in
        # django-stubs; return type is QuerySet, not TenantQuerySet.
        clone: TenantQuerySet = super()._clone()  # type: ignore[misc]
        clone._rls_user = self._rls_user
        return clone

    # ---- Strict mode guard ----

    def _check_strict_mode(self) -> None:
        """Raise if strict mode is on and no RLS context is active.

        Only applies to RLS-protected models (those with an
        ``RLSConstraint``). Non-protected models that happen to use
        ``TenantQuerySet`` (e.g., via inheritance) are not guarded.

        The check reads two ``ContextVar`` values -- negligible cost.

        Raises:
            NoTenantContextError: If ``STRICT_MODE=True`` and neither a
                context manager/middleware nor ``for_user()`` has
                established an RLS context.
        """
        if not rls_tenants_config.STRICT_MODE:
            return
        if not _is_rls_protected(self.model):
            return  # not an RLS-protected model; skip guard
        if get_rls_context_active():
            return  # tenant_context / admin_context / middleware active
        if self._rls_user is not None:
            return  # for_user() was called
        model_name = self.model.__name__
        msg = f"RLS strict mode: query on {model_name} attempted without an active RLS context."
        hint = f"{HINT_NO_CONTEXT} To disable this guard, set STRICT_MODE=False in RLS_TENANTS."
        raise NoTenantContextError(msg, hint=hint)

    # ---- for_user() RLS scope ----

    @contextmanager
    def _rls_scope(
        self,
        *,
        for_write: bool = False,
        using: str | None = None,
        own_transaction: bool = False,
    ) -> Iterator[None]:
        """Apply the ``for_user()`` user's RLS context around SQL this queryset runs.

        A no-op unless ``for_user()`` was called. Otherwise, on the
        connection the SQL runs on, it:

        1. reads the admin and tenant GUC variables;
        2. sets those that differ from the user's values, so nothing is
           written when an enclosing context (``RLSTenantMiddleware`` for
           the same user, the scope of an outer call) already applied them;
        3. sets the tenant state ``ContextVar`` values, so querysets created
           meanwhile (prefetches, ``delete()`` cascades, signal handlers) are
           auto-scoped to the user's tenant and pass ``STRICT_MODE``;
        4. on exit, resets the ``ContextVar`` values and restores the GUCs it
           changed to the values read in step 1.

        Restoring instead of clearing leaves an enclosing ``tenant_context()``,
        ``admin_context()`` or middleware context intact after a ``for_user()``
        query, in both GUC modes, and lets scopes nest. While an exception
        propagates out of an aborted transaction a failing restore is logged
        and dropped, and the rollback undoes the change (see
        :func:`django_rls_tenants.rls.context.restore_gucs`).

        With ``USE_LOCAL_SET=True``, steps 1, 2 and 4's restore are skipped on
        an alias in ``RLS_TENANTS["DATABASES"]``, inside
        ``transaction.atomic()`` or not: the GUC affinity guard sets the
        user's values transaction-locally before the first statement that
        needs them, and on exit inside a transaction it puts the enclosing
        values back (see ``context._writes_gucs()``).

        The alias is resolved once and pinned on the queryset for the
        duration of the scope, so Django runs the SQL where the GUCs were
        set even when a database router answers differently on each call.

        Args:
            for_write: Target the alias Django writes to instead of
                ``self.db``. Write methods set ``_for_write`` only once they
                run, so before that ``self.db`` names the read alias.
            using: Alias to use instead of resolving it (``iterator()``
                resolves it once for all its chunks).
            own_transaction: With ``USE_LOCAL_SET=True`` and no transaction
                open, run the scope in ``transaction.atomic()``. For methods
                Django runs in a transaction of its own anyway, so the whole
                call shares one: on an alias in ``RLS_TENANTS["DATABASES"]``
                the GUC affinity guard sets the values once in it, and on any
                other alias the scope's own ``SET LOCAL`` needs it.

        Raises:
            NoTenantContextError: If the user is not an admin and has
                ``rls_tenant_id=None``.
            RuntimeError: If ``USE_LOCAL_SET=True``, a GUC has to change, the
                alias is not in ``RLS_TENANTS["DATABASES"]`` and its
                connection is not inside ``transaction.atomic()`` (and
                ``own_transaction`` is not set).
        """
        user = self._rls_user
        if user is None:
            yield
            return
        conf = rls_tenants_config
        is_local = conf.USE_LOCAL_SET
        if using is None:
            using = self._db_for_write() if for_write else self.db
        variables = _resolve_user_guc_vars(user, conf)
        # False with USE_LOCAL_SET on an alias the guard covers: the guard writes
        # the values (for own_transaction, once, inside the transaction below).
        writes = _writes_gucs(using, is_local=is_local)
        transaction_block = (
            transaction.atomic(using=using, savepoint=False)
            if own_transaction and is_local and not connections[using].in_atomic_block
            else nullcontext()
        )
        with self._pinned_db(using), transaction_block:
            # None: nothing is written, so nothing is restored.
            changed: dict[str, str | None] | None = None
            if writes:
                previous = _snapshot_gucs(conf, using=using)
                # Decided before writing, so every GUC that may have been set is
                # restored, even when an exception lands right after a set_guc().
                changed = {
                    name: previous[name]
                    for name, value in variables.items()
                    # get_guc() reads an empty variable as None; the admin tenant value is "".
                    if (previous[name] or "") != value
                }
            gucs = _context_gucs_of(variables, conf)
            tenant_id = None if user.is_tenant_admin else user.rls_tenant_id
            session = _session_of(using)
            enclosing = _current_live()
            exc_pending = False
            try:
                with _rls_state(tenant_id, gucs):
                    if changed is not None:
                        with writing(using, gucs, local=is_local):
                            for name in changed:
                                set_guc(name, variables[name], is_local=is_local, using=using)
                    yield
            except BaseException:
                exc_pending = True
                raise
            finally:
                _restore_tenant_gucs(
                    changed,
                    is_local=is_local,
                    using=using,
                    exc_pending=exc_pending,
                    enclosing=enclosing,
                    session=session,
                )

    @contextmanager
    def _pinned_db(self, using: str) -> Iterator[None]:
        """Make ``self.db`` return ``using`` inside the block, whatever a router answers."""
        pinned_db = self._db
        self._db = using
        try:
            yield
        finally:
            self._db = pinned_db

    def _db_for_write(self) -> str:
        """Return the alias a write on this queryset runs on.

        This is ``QuerySet.db`` as Django computes it once a write method
        has set ``_for_write``; with a read/write router it differs from
        ``self.db``.
        """
        return self._db or router.db_for_write(self.model, **self._hints)

    # ---- Guarded evaluation methods ----
    #
    # Each override below checks STRICT_MODE, runs its SQL inside
    # _rls_scope(), or both; its docstring says which. get(), first() and
    # last() only check: their SQL runs on a clone, in _fetch_all()'s scope.
    # Methods that are not overridden (get_or_create(), contains(),
    # in_bulk(), the async a*() wrappers, ...) run their SQL through these
    # methods, on a clone that carries _rls_user.
    #
    # A queryset whose results are cached answers some calls without SQL;
    # those skip both the scope and the check. Otherwise get(), first() and
    # last() are checked twice, here and in _fetch_all() on the clone. This
    # is intentional defense-in-depth: the direct check catches the call at
    # the public API boundary, while the _fetch_all() check catches any code
    # path that reaches evaluation without going through these overrides.

    def count(self) -> int:
        """Run ``count()`` in the ``for_user()`` scope; guarded by strict mode.

        A queryset with cached results answers from the cache, so it runs
        no SQL and needs neither the scope nor the strict mode check.
        """
        if self._result_cache is not None:
            return super().count()
        self._check_strict_mode()
        with self._rls_scope():
            return super().count()

    def exists(self) -> bool:
        """Run ``exists()`` in the ``for_user()`` scope; guarded by strict mode.

        A queryset with cached results answers from the cache, so it runs
        no SQL and needs neither the scope nor the strict mode check.
        """
        if self._result_cache is not None:
            return super().exists()
        self._check_strict_mode()
        with self._rls_scope():
            return super().exists()

    def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run ``aggregate()`` in the ``for_user()`` scope; guarded by strict mode."""
        self._check_strict_mode()
        with self._rls_scope():
            return super().aggregate(*args, **kwargs)

    def update(self, **kwargs: Any) -> int:
        """Run ``update()`` in the ``for_user()`` scope; guarded by strict mode."""
        self._check_strict_mode()
        with self._rls_scope(for_write=True):
            return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        """Run ``delete()`` in the ``for_user()`` scope; guarded by strict mode.

        One scope covers the whole deletion: collecting the rows, the
        cascades and ``SET_NULL`` updates on related models, the delete
        signals and the ``DELETE`` statements.
        """
        self._check_strict_mode()
        with self._rls_scope(for_write=True, own_transaction=True):
            return super().delete()

    # QuerySet.delete() is queryset_only, so a manager has no delete() that
    # empties the whole table. Overriding the method drops the flag; restore it.
    delete.queryset_only = True  # type: ignore[attr-defined]

    def iterator(self, chunk_size: int | None = None) -> Iterator[Any]:
        """Run ``iterator()`` in the ``for_user()`` scope; guarded by strict mode.

        Each chunk of ``chunk_size`` rows (Django's default of 2000 when
        ``None``) is fetched, together with its ``prefetch_related()``
        lookups, inside its own scope. The scope is not held while the
        caller's loop body runs, so the user's context neither leaks into
        that code nor stays on the connection when the loop stops early.
        """
        self._check_strict_mode()
        if self._rls_user is None:
            return super().iterator(chunk_size=chunk_size)
        # Resolved once: the cursor and every chunk's scope use the same alias.
        using = self.db
        with self._pinned_db(using):
            rows = super().iterator(chunk_size=chunk_size)
        return self._scoped_chunks(rows, chunk_size or _ITERATOR_CHUNK_SIZE, using)

    def _scoped_chunks(self, rows: Iterator[Any], chunk_size: int, using: str) -> Iterator[Any]:
        """Yield from ``rows``, pulling every chunk inside :meth:`_rls_scope`.

        Every ``next()`` on ``rows`` that can execute SQL (opening the
        cursor, fetching the next chunk, prefetching) happens inside a
        scope. A server-side cursor inside a transaction produces its rows
        at fetch time, and a part of the plan that a later fetch reaches
        first (a ``UNION ALL`` branch, say) evaluates the RLS policy only
        then, so each fetch needs the user's GUCs.
        """
        while True:
            with self._rls_scope(using=using):
                chunk = _next_chunk(rows, chunk_size)
            yield from chunk
            if len(chunk) < chunk_size:
                return

    async def aiterator(self, chunk_size: int = _ITERATOR_CHUNK_SIZE) -> AsyncIterator[Any]:
        """Run ``aiterator()`` in the ``for_user()`` scope; guarded by strict mode.

        Django's ``aiterator()`` does not go through ``iterator()``. With a
        ``for_user()`` user, each chunk is pulled from :meth:`iterator` in
        one thread-sensitive ``sync_to_async`` hop, so every scope is
        entered and left inside a hop, on the thread that owns the
        connection.
        """
        if self._rls_user is None:
            self._check_strict_mode()
            async for item in super().aiterator(chunk_size=chunk_size):
                yield item
            return
        if chunk_size <= 0:  # QuerySet.aiterator()'s check and message
            msg = "Chunk size must be strictly positive."
            raise ValueError(msg)
        # iterator() also runs the strict mode check, which for_user() satisfies.
        rows = self.iterator(chunk_size=chunk_size)
        while True:
            chunk = await sync_to_async(_next_chunk, thread_sensitive=True)(rows, chunk_size)
            for item in chunk:
                yield item
            if len(chunk) < chunk_size:
                return

    def create(self, **kwargs: Any) -> Any:
        """Run ``create()`` in the ``for_user()`` scope.

        The ``INSERT`` must pass the RLS policy's ``WITH CHECK`` clause,
        which reads the GUCs. Unlike ``bulk_create()``, which has been
        guarded since strict mode was added, ``create()`` is not: without a
        context the database rejects the row loudly instead of returning a
        wrong result, and guarding it now would break code that creates
        rows outside a context on a superuser connection.
        """
        with self._rls_scope(for_write=True):
            return super().create(**kwargs)

    def bulk_create(
        self,
        objs: Any,
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Any = None,
        unique_fields: Any = None,
    ) -> list[Any]:
        """Run ``bulk_create()`` in the ``for_user()`` scope; guarded by strict mode."""
        self._check_strict_mode()
        with self._rls_scope(for_write=True, own_transaction=True):
            return super().bulk_create(
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

    def bulk_update(
        self,
        objs: Any,
        fields: Any,
        batch_size: int | None = None,
    ) -> int:
        """Run ``bulk_update()`` in one ``for_user()`` scope; guarded by strict mode."""
        self._check_strict_mode()
        with self._rls_scope(for_write=True, own_transaction=True):
            return super().bulk_update(objs, fields, batch_size=batch_size)

    def update_or_create(
        self,
        defaults: Mapping[str, Any] | None = None,
        create_defaults: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        """Run ``update_or_create()`` in the ``for_user()`` scope.

        Updating an existing row calls ``Model.save()``, which writes
        through the model's base manager rather than this queryset, so one
        scope spans the whole call. The lookup runs through ``_fetch_all()``
        and is guarded by strict mode there.
        """
        with self._rls_scope(for_write=True, own_transaction=True):
            return super().update_or_create(
                defaults=defaults,
                create_defaults=create_defaults,
                **kwargs,
            )

    def explain(
        self,
        *,
        format: str | None = None,  # noqa: A002  -- QuerySet.explain()'s parameter name
        **options: Any,
    ) -> str:
        """Run ``explain()`` in the ``for_user()`` scope.

        With ``analyze=True`` the query runs, and the plan then reports the
        user's rows. Not guarded by strict mode: it is a diagnostic.
        """
        with self._rls_scope():
            return super().explain(format=format, **options)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Guard ``get()`` with strict mode check."""
        self._check_strict_mode()
        return super().get(*args, **kwargs)

    def first(self) -> Any:
        """Guard ``first()`` with strict mode check, unless the results are cached.

        On a cached, ordered queryset Django answers from the cache. Any
        other case queries a clone, which ``_fetch_all()`` checks.
        """
        if self._result_cache is None:
            self._check_strict_mode()
        return super().first()

    def last(self) -> Any:
        """Guard ``last()`` with strict mode check.

        It always queries: Django reverses the ordering on a fresh clone.
        """
        self._check_strict_mode()
        return super().last()

    def _fetch_all(self) -> None:
        """Evaluate in the ``for_user()`` scope; guarded by strict mode.

        The scope uses ``self.db``, so the GUCs target the connection the
        query runs on in multi-database setups (``.using("replica")``, a
        read router).

        A queryset whose results and prefetches are already cached runs no
        SQL, so it skips both the scope and the strict mode check. That
        includes the querysets ``prefetch_related()`` caches on each
        instance: they can be read after the context that fetched them has
        ended, for example in a template rendering a ``for_user()``
        queryset.
        """
        if self._result_cache is not None and (
            self._prefetch_done or not self._prefetch_related_lookups
        ):
            super()._fetch_all()
            return
        self._check_strict_mode()
        with self._rls_scope():
            super()._fetch_all()


def _resolve_related_path(
    model: type[models.Model],
    field_path: str,
) -> tuple[type[models.Model] | None, bool]:
    """Walk a dotted field path to find the target model and its nullability.

    For ``"category__parent"``, starts at ``model``, follows ``category``
    to its related model, then follows ``parent`` to the final model.

    Args:
        model: The starting Django model class.
        field_path: A ``__``-separated relation path (e.g., ``"category"``
            or ``"order__customer"``).

    Returns:
        A ``(target_model, nullable)`` pair. ``target_model`` is ``None``
        if the path is invalid. ``nullable`` is ``True`` when **any**
        segment of the path can be ``NULL`` (a nullable forward FK or a
        reverse one-to-one), i.e. when a join along the path must stay a
        LEFT OUTER JOIN to keep rows whose chain is broken by a ``NULL``.
    """
    current: type[models.Model] = model
    nullable = False
    for part in field_path.split("__"):
        try:
            field = current._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            return None, False
        related = getattr(field, "related_model", None)
        if related is None or isinstance(related, str):
            # str means lazy reference like "self" -- skip
            return None, False
        nullable = nullable or bool(getattr(field, "null", False))
        current = related
    return current, nullable


def _resolve_related_model(
    model: type[models.Model],
    field_path: str,
) -> type[models.Model] | None:
    """Walk a dotted field path to find the target model.

    Thin wrapper around :func:`_resolve_related_path` for callers that
    only need the model.

    Returns:
        The target model class, or ``None`` if the path is invalid.
    """
    return _resolve_related_path(model, field_path)[0]


# Type ignore explanation: Same as TenantQuerySet: Manager is generic at runtime
# but unparameterised in django-stubs.
class RLSManager(models.Manager):  # type: ignore[type-arg]
    """Manager for RLS-protected models.

    Provides ``for_user()`` for scoped queries and
    ``prepare_tenant_in_model_data()`` for resolving tenant FKs.
    """

    # Django's router hints (``db_manager(hints=...)``); django-stubs does not declare it.
    _hints: dict[str, Any]

    def get_queryset(self) -> TenantQuerySet:
        """Return a ``TenantQuerySet`` instance, auto-scoped if a tenant context is active.

        When ``tenant_context()``, ``admin_context()``, or ``RLSTenantMiddleware``
        has set a current tenant ID, the queryset is automatically filtered by
        ``WHERE tenant_id = X``. This enables PostgreSQL to use composite indexes
        instead of relying solely on RLS ``current_setting()`` calls.
        """
        # hints too, as BaseManager.get_queryset() passes them: a router reads
        # them (db_manager(hints=...), the hint Django adds for related objects).
        qs = TenantQuerySet(self.model, using=self._db, hints=self._hints)
        tenant_id = get_current_tenant_id()
        if tenant_id is not None:
            conf = rls_tenants_config
            qs = qs.filter(**{f"{conf.TENANT_FK_FIELD}_id": tenant_id})
        return qs

    def for_user(self, as_user: TenantUser) -> TenantQuerySet:
        """Return a queryset scoped to the given user's tenant."""
        return self.get_queryset().for_user(as_user=as_user)

    def prepare_tenant_in_model_data(
        self,
        model_data: dict[str, Any],
        # Unused but part of the public API contract.
        as_user: TenantUser,  # noqa: ARG002
    ) -> None:
        """Resolve a raw tenant ID for model creation.

        If ``model_data`` contains a raw tenant ID (int/str) under
        the configured FK field name, sets the FK column directly
        (``{field}_id``) to avoid a ``SELECT`` query. Allows passing
        ``tenant=42`` in creation data without N+1 overhead.

        Args:
            model_data: Dict of field names to values.
            as_user: User for context (unused here but part of API).
        """
        # Lazy import avoids a circular import.
        from django.apps import apps  # noqa: PLC0415

        conf = rls_tenants_config
        field_name = conf.TENANT_FK_FIELD
        tenant_model = apps.get_model(conf.TENANT_MODEL)

        tenant = model_data.get(field_name)
        if tenant is not None and not isinstance(tenant, tenant_model):
            # Set the FK column directly to avoid a model fetch query.
            # For bulk creates this eliminates N identical SELECTs.
            fk_column = f"{field_name}_id"
            model_data[fk_column] = tenant
            del model_data[field_name]
