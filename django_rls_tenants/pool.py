"""Hygiene for connections that outlive a request, such as those in Django's psycopg pool.

With ``OPTIONS["pool"]`` (Django 5.1+, psycopg 3), closing a connection hands it back to a
``psycopg_pool.ConnectionPool``, which only rolls back an open transaction. Session GUCs
set with ``set_config()`` survive into the next checkout. Normally the request's cleanup
has cleared them, but not when the cleanup failed or reached a different connection: a
view that closed its connection early, a crash, a cancelled request.

Two defences use the same single-statement clear of every GUC the library's RLS policies
read (:func:`_policy_guc_names`):

- the ``connection_created`` handler in ``apps.py`` runs it on every checkout from a pool
  on an alias in ``RLS_TENANTS["DATABASES"]``, before any query of the new borrower, and
  discards the connection if it fails;
- :func:`reset_rls_gucs`, passed as the pool's ``reset`` callback, runs it when the
  connection is returned. System check ``W010`` asks for it.

This module is imported from ``settings.py``, before Django is set up, so it must not
import ``django_rls_tenants.tenants`` (whose ``__init__`` imports models) at module level.
"""

from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING, Any, Protocol

from django.apps import apps

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from django.db.backends.base.base import BaseDatabaseWrapper

# A name PostgreSQL accepts for a custom (placeholder) GUC: two or more identifiers
# joined by dots. ``set_config()`` rejects any other name unless it is a built-in
# setting, which is not the library's to clear.
_CUSTOM_GUC_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+")


class _PoolConnection(Protocol):
    """The part of a psycopg 3 ``Connection`` that :func:`reset_rls_gucs` uses."""

    autocommit: bool

    def execute(self, query: str, params: Sequence[str], /) -> object:
        """Run ``query`` with ``params``."""

    def commit(self) -> None:
        """Commit the current transaction."""


def reset_rls_gucs(conn: _PoolConnection) -> None:
    """Clear the RLS GUCs on a connection returned to Django's psycopg pool.

    Pass it as the pool's ``reset`` callback; Django forwards every key of
    ``OPTIONS["pool"]`` to ``psycopg_pool.ConnectionPool``::

        from django_rls_tenants.pool import reset_rls_gucs

        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                # ...
                "OPTIONS": {"pool": {"reset": reset_rls_gucs}},
            },
        }

    The pool calls it on the raw psycopg connection, in one of its worker
    threads, after rolling back any transaction left open. It clears every GUC
    the library's RLS policies read (the configured tenant and admin GUCs, and
    each ``RLSConstraint``'s GUCs and ``extra_bypass_flags``) in one statement
    and leaves the connection idle, as the pool requires. If it raises, the pool
    discards the connection instead of reusing it.

    Unlike ``DISCARD ALL`` or ``RESET ALL``, it keeps the session settings
    Django applies only once per pooled connection, such as the time zone.

    Args:
        conn: The psycopg 3 connection being returned to the pool.
    """
    names = _policy_guc_names()
    if not names:
        return
    conn.execute(_build_clear_gucs_sql(len(names)), names)
    if not conn.autocommit:
        # Outside autocommit the statement opened a transaction; the pool
        # discards a connection that is not idle when this returns.
        conn.commit()


def _build_clear_gucs_sql(count: int) -> str:
    """Build one statement that clears ``count`` session GUCs, named by bind parameters."""
    return "SELECT " + ", ".join(["set_config(%s, '', false)"] * count)


def _clear_policy_gucs(connection: BaseDatabaseWrapper) -> None:
    """Clear every GUC the library's RLS policies read on a connection, in one round trip.

    Outside autocommit (``AUTOCOMMIT=False``) the statement is committed, as
    Django commits its own connection setup, so a later rollback cannot bring
    the previous values back.

    Args:
        connection: The Django connection, as sent by ``connection_created``.
    """
    names = _policy_guc_names()
    if not names:
        return
    with connection.cursor() as cursor:
        cursor.execute(_build_clear_gucs_sql(len(names)), names)
    if not connection.get_autocommit():
        connection.connection.commit()


def _policy_guc_names() -> tuple[str, ...]:
    """Every GUC the library's RLS policies read, without duplicates.

    The tenant and admin GUCs derived from ``GUC_PREFIX`` come first (read on
    every call, so a settings override applies), then the names the installed
    models' ``RLSConstraint`` and ``RLSM2MConstraint`` declare. Those can differ
    from the configured ones (system checks ``W001`` / ``W002``), and include
    every ``extra_bypass_flags`` entry. GUCs read by hand-written policies, or
    by ``AddM2MRLSPolicy`` with non-default names, are not covered.

    Only custom (dotted) GUC names are kept (``_CUSTOM_GUC_NAME_RE``).
    ``set_config()`` rejects any other name, such as a flag without a dot,
    which would fail the whole clear statement, unless it is a built-in
    setting such as ``search_path``, which is not the library's to clear.
    """
    # tenants/__init__ imports models, which fails before Django is set up.
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    # Cache only a complete registry: register_m2m_rls() adds constraints in
    # this library's AppConfig.ready(), and apps.ready turns true after every ready().
    declared = _declared_policy_guc_names_cached() if apps.ready else _declared_policy_guc_names()
    names = (rls_tenants_config.GUC_IS_ADMIN, rls_tenants_config.GUC_CURRENT_TENANT, *declared)
    return tuple(name for name in dict.fromkeys(names) if _CUSTOM_GUC_NAME_RE.fullmatch(name))


def _declared_policy_guc_names() -> tuple[str, ...]:
    """The GUCs the RLS constraints on the installed models read, without duplicates."""
    # Loads the rls package only when needed, keeping the settings-time import light.
    from django_rls_tenants.rls.constraints import (  # noqa: PLC0415
        RLSConstraint,
        RLSM2MConstraint,
    )

    names: list[str] = []
    # include_auto_created: register_m2m_rls() puts RLSM2MConstraint on the
    # auto-created M2M through models.
    for model in apps.get_models(include_auto_created=True):
        for constraint in model._meta.constraints:  # noqa: SLF001  -- Django's public Meta API
            if isinstance(constraint, RLSConstraint):
                names += [
                    constraint.guc_tenant_var,
                    constraint.guc_admin_var,
                    *constraint.extra_bypass_flags,
                ]
            elif isinstance(constraint, RLSM2MConstraint):
                names += [constraint.guc_tenant_var, constraint.guc_admin_var]
    # Deduplicated here, once, so the per-checkout work does not grow with the models.
    return tuple(dict.fromkeys(names))


_declared_policy_guc_names_cached = functools.cache(_declared_policy_guc_names)


def _pool_options(settings_dict: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return a database's connection-pool options, or ``None`` when it is not pooled.

    Mirrors Django's own test: a falsy ``OPTIONS["pool"]`` (including ``{}``)
    disables the pool, and ``True`` enables it with default options.

    Args:
        settings_dict: One entry of ``settings.DATABASES``, or a connection's
            ``settings_dict``.
    """
    pool = settings_dict.get("OPTIONS", {}).get("pool")
    if not pool:
        return None
    return {} if pool is True else pool
