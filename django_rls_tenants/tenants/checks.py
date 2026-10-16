"""Django system checks for RLS tenant configuration.

Detects configuration issues at startup that would otherwise cause
silent failures at runtime:

- Superuser or ``BYPASSRLS`` database connections (RLS is completely bypassed): a
  warning with ``DEBUG`` on, the ``E001`` error with it off.
- A database- or role-level default (``ALTER DATABASE/ROLE ... SET``) for a GUC the
  RLS policies read, in force in every new session before any context is set.
- Either of those two could not be determined on a reachable database (``W013``):
  silence from a check that could not run is not a pass.
- ``GUC_PREFIX`` mismatch between runtime config and ``RLSConstraint`` defaults.
- ``CONN_MAX_AGE > 0`` with ``USE_LOCAL_SET=False`` (connection-pool GUC leak risk).
- Django's connection pool with ``USE_LOCAL_SET=False`` and no pool ``reset``.
- ``DATABASES`` contains aliases not in ``settings.DATABASES``.
- ``TENANT_MODEL`` does not resolve to an installed model.
- ``TENANT_FK_FIELD`` missing on an ``RLSProtectedModel`` subclass.
- A unique field or constraint on an RLS-protected model that omits the tenant
  field (``W012``): unique indexes are checked outside the RLS policies, so the
  ``IntegrityError`` tells one tenant that a value exists in another.

Check IDs are never renumbered. ``W003`` (retired in 1.3.1) and ``W007``
(retired in 1.3.2) are no longer emitted: ``RLSTenantMiddleware`` opens its
own transaction when ``USE_LOCAL_SET=True`` (and since 1.4.0 the GUC affinity
guard gives each statement run in autocommit inside a context a transaction of
its own in that mode), so ``ATOMIC_REQUESTS`` is no longer a requirement of
that mode.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from django.core.checks import CheckMessage, register
from django.core.checks import Error as CheckError
from django.core.checks import Warning as CheckWarning
from django.core.exceptions import ImproperlyConfigured
from django.db import Error, InterfaceError, OperationalError, connections

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from django.db import models

logger = logging.getLogger("django_rls_tenants")


@register("django_rls_tenants")
def check_rls_config(
    app_configs: Sequence[Any] | None = None,  # noqa: ARG001  -- Django check API
    **kwargs: Any,  # noqa: ARG001  -- Django check API
) -> list[CheckMessage]:
    """Run all RLS configuration checks."""
    errors: list[CheckMessage] = []
    errors.extend(_check_superuser_connection())
    errors.extend(_check_policy_guc_defaults())
    errors.extend(_check_guc_prefix_mismatch())
    errors.extend(_check_conn_max_age_with_session_gucs())
    errors.extend(_check_pool_without_reset())
    errors.extend(_check_databases_alias_exists())
    errors.extend(_check_tenant_model_resolves())
    errors.extend(_check_tenant_fk_field_exists())
    errors.extend(_check_unique_across_tenants())
    return errors


def _checked_aliases() -> list[str]:
    """Return the aliases in ``RLS_TENANTS["DATABASES"]`` the database checks query.

    An alias missing from ``settings.DATABASES`` is left to ``W006``.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    django_databases = getattr(settings, "DATABASES", {})
    return [alias for alias in rls_tenants_config.DATABASES if alias in django_databases]


def _read_catalogue(
    alias: str,
    sql: str,
    *,
    subject: str,
    unconfirmed: str,
) -> list[tuple[Any, ...]] | CheckWarning | None:
    """Run a catalogue query on ``alias`` for one of the database checks.

    Returns the rows, or ``None`` when the check does not apply: the alias is not
    a PostgreSQL database, or no connection could be opened (the database may not
    be provisioned yet, or a command that needs none runs without one; logged at
    ``DEBUG``). When the database was reached but the query failed (a missing
    privilege, a failing connection hook), the check could not run, and a clean
    ``check`` must not be read as a pass: that returns ``W013``, naming the
    exception, ``subject`` (what could not be determined) and ``unconfirmed``
    (the check whose absence proves nothing).

    Inside a transaction (``call_command("check")`` in a test, for one) the query
    runs in a savepoint, so a failure aborts only that savepoint and the caller's
    transaction stays usable; in autocommit no savepoint is needed.
    """
    connection = connections[alias]
    if connection.vendor != "postgresql":
        return None
    try:
        cursor = connection.cursor()
    except (ImproperlyConfigured, InterfaceError, OperationalError):
        logger.debug(
            "Skipping %s on DATABASES[%r]: could not connect (database may be unavailable).",
            unconfirmed,
            alias,
            exc_info=True,
        )
        return None
    except Error as exc:
        return _undetermined(alias, exc, subject=subject, unconfirmed=unconfirmed)
    try:
        savepoint = connection.savepoint()  # None outside a transaction
        try:
            with cursor:
                cursor.execute(sql)
                rows = list(cursor.fetchall())
        except Error:
            if savepoint is not None:
                connection.savepoint_rollback(savepoint)
            raise
        if savepoint is not None:
            connection.savepoint_commit(savepoint)
    except Error as exc:
        return _undetermined(alias, exc, subject=subject, unconfirmed=unconfirmed)
    return rows


def _describe_error(exc: BaseException) -> str:
    """Name ``exc`` and the first line of its message (drivers add context lines)."""
    detail = str(exc).strip()
    return type(exc).__name__ + (f": {detail.splitlines()[0]}" if detail else "")


def _undetermined(alias: str, exc: Exception, *, subject: str, unconfirmed: str) -> CheckWarning:
    """Build the ``W013`` warning for a database check that could not run."""
    return CheckWarning(
        f"Could not determine {subject} on DATABASES[{alias!r}]: the query failed "
        f"with {_describe_error(exc)}. Do not read the absence of {unconfirmed} as a pass.",
        hint=(
            f"DATABASES[{alias!r}] was reachable, but the check's catalogue query did "
            f"not complete. Fix the cause named above (often a missing privilege on a "
            f"pg_catalog view) and run `python manage.py check` again."
        ),
        id="django_rls_tenants.W013",
    )


def _check_superuser_connection() -> list[CheckMessage]:
    """Report every configured alias whose connection role bypasses RLS.

    Two role attributes make PostgreSQL skip Row-Level Security entirely,
    even with ``FORCE ROW LEVEL SECURITY``:

    - ``SUPERUSER`` -- superusers bypass every policy.
    - ``BYPASSRLS`` -- a non-superuser role with this attribute bypasses
      every policy exactly like a superuser.

    If the Django application connects as such a role, tenant isolation is
    completely disabled -- every query returns all rows regardless of GUC
    settings. Both attributes are read from ``pg_roles`` (which, unlike
    ``pg_user``, also lists ``NOLOGIN`` roles reached via ``SET ROLE``), on
    every alias in ``RLS_TENANTS["DATABASES"]``.

    With ``settings.DEBUG`` on this is ``W005``; with it off (a production
    deployment, but also Django's test runner) it is the ``E001`` error, which
    fails ``check`` and every command that runs the system checks. A query that
    fails on a reachable database gives ``W013`` (see ``_read_catalogue()``).
    """
    from django.conf import settings  # noqa: PLC0415

    messages: list[CheckMessage] = []
    for alias in _checked_aliases():
        rows = _read_catalogue(
            alias,
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
            subject="whether the connection role bypasses RLS",
            unconfirmed="W005 and E001",
        )
        if not isinstance(rows, list):
            if rows is not None:
                messages.append(rows)
            continue
        if not rows:
            continue
        role, is_superuser, bypasses_rls = rows[0]
        reasons: list[str] = []
        if is_superuser:
            reasons.append("is a PostgreSQL superuser")
        if bypasses_rls:
            reasons.append("has the BYPASSRLS attribute")
        if not reasons:
            continue
        quoted_role = connections[alias].ops.quote_name(role)
        msg = (
            f"The connection role {role!r} of DATABASES[{alias!r}] "
            f"{' and '.join(reasons)}. Superusers and BYPASSRLS roles bypass ALL "
            "Row-Level Security policies, completely disabling tenant isolation."
        )
        hint = (
            "Connect Django as a role with NOSUPERUSER and NOBYPASSRLS and set it in "
            f"DATABASES[{alias!r}]. Example: CREATE ROLE app LOGIN PASSWORD 'secret' "
            "NOSUPERUSER NOBYPASSRLS; GRANT ALL ON ALL TABLES IN SCHEMA public TO app; "
            f"-- or fix the existing role: ALTER ROLE {quoted_role} NOSUPERUSER NOBYPASSRLS;"
        )
        if settings.DEBUG:
            messages.append(CheckWarning(msg, hint=hint, id="django_rls_tenants.W005"))
            continue
        messages.append(
            CheckError(
                f"{msg} settings.DEBUG is False, so this is treated as a production deployment.",
                hint=(
                    f"{hint} If these settings must use such a role (a test or migration "
                    "settings module), record that with "
                    "SILENCED_SYSTEM_CHECKS = ['django_rls_tenants.E001']."
                ),
                id="django_rls_tenants.E001",
            )
        )
    return messages


def _check_policy_guc_defaults() -> list[CheckWarning]:
    """Warn if PostgreSQL applies a default value for a GUC the RLS policies read.

    ``ALTER DATABASE ... SET rls.is_admin = 'true'`` or ``ALTER ROLE ... SET``
    makes PostgreSQL set that value at the start of every session of that
    database or role. The policies then read it before any RLS context is set:
    an admin default bypasses them in every session, and a tenant default
    shows that tenant's rows to everyone. The library assumes a new session
    holds no value for these GUCs (the GUC affinity guard clears a session's
    own values only after a context has used it), so such a default is never
    cleared. Reads ``pg_db_role_setting`` on every alias in
    ``RLS_TENANTS["DATABASES"]``, for the connected database and for
    cluster-wide (role-only) defaults: role-only defaults first, then the
    database's, then those of a role in the database. A query that fails on a
    reachable database gives ``W013`` (see ``_read_catalogue()``).
    """
    from django_rls_tenants.pool import _policy_guc_names  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    names = {name.lower() for name in _policy_guc_names()}
    if not names:
        return warnings
    for alias in _checked_aliases():
        rows = _read_catalogue(
            alias,
            "SELECT d.datname, r.rolname, s.setconfig "
            "FROM pg_db_role_setting AS s "
            "LEFT JOIN pg_database AS d ON d.oid = s.setdatabase "
            "LEFT JOIN pg_roles AS r ON r.oid = s.setrole "
            "WHERE s.setdatabase = 0 OR d.datname = current_database() "
            "ORDER BY d.datname NULLS FIRST, r.rolname NULLS FIRST",
            subject="whether PostgreSQL applies a default for a GUC the RLS policies read",
            unconfirmed="W011",
        )
        if not isinstance(rows, list):
            if rows is not None:
                warnings.append(rows)
            continue
        warnings.extend(_policy_guc_default_warnings(alias, rows, names))
    return warnings


def _policy_guc_default_warnings(
    alias: str,
    rows: list[tuple[Any, ...]],
    names: set[str],
) -> list[CheckWarning]:
    """Build one ``W011`` per default in ``rows`` for a GUC in ``names`` (lower-case)."""
    quote = connections[alias].ops.quote_name
    warnings: list[CheckWarning] = []
    for database, role, setconfig in rows:
        for entry in setconfig or ():
            name, _, value = entry.partition("=")
            if name.lower() not in names:
                continue
            if role is None:
                scope = f"database {database!r}"
                target = f"DATABASE {quote(database)}"
            elif database is None:
                scope = f"role {role!r} in every database"
                target = f"ROLE {quote(role)}"
            else:
                scope = f"role {role!r} in database {database!r}"
                target = f"ROLE {quote(role)} IN DATABASE {quote(database)}"
            warnings.append(
                CheckWarning(
                    f"PostgreSQL sets {name} = {value!r} at the start of every session "
                    f"for {scope} (DATABASES[{alias!r}]). The RLS policies read this GUC, "
                    f"so every session starts with that value in force instead of no RLS "
                    f"context (fail-closed), and django-rls-tenants never clears it: an "
                    f"admin value bypasses the policies, a tenant value shows that "
                    f"tenant's rows to everyone.",
                    hint=(
                        f"Remove the default: ALTER {target} RESET {name}; "
                        f"and set the RLS context per request or block instead."
                    ),
                    id="django_rls_tenants.W011",
                )
            )
    return warnings


def _check_guc_prefix_mismatch() -> list[CheckWarning]:
    """Warn if ``GUC_PREFIX`` differs from the ``RLSConstraint`` defaults.

    If the user changes ``GUC_PREFIX`` (e.g., to ``"myapp"``), the middleware
    will set ``myapp.current_tenant`` while the database policy still checks
    ``rls.current_tenant``. Tenant isolation silently breaks.
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.rls.constraints import RLSConstraint  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.models import RLSProtectedModel  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    runtime_tenant_guc = rls_tenants_config.GUC_CURRENT_TENANT
    runtime_admin_guc = rls_tenants_config.GUC_IS_ADMIN

    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model._meta.abstract:  # noqa: SLF001
            continue
        for constraint in model._meta.constraints:  # noqa: SLF001
            if not isinstance(constraint, RLSConstraint):
                continue
            if constraint.guc_tenant_var != runtime_tenant_guc:
                warnings.append(
                    CheckWarning(
                        f"RLSConstraint on {model.__name__} uses "
                        f"guc_tenant_var={constraint.guc_tenant_var!r} but "
                        f"GUC_PREFIX derives {runtime_tenant_guc!r}. "
                        f"Tenant isolation will silently break.",
                        hint=(
                            f"Either pass guc_tenant_var={runtime_tenant_guc!r} to "
                            f"the RLSConstraint, or set RLS_TENANTS['GUC_PREFIX'] so "
                            f"it derives {runtime_tenant_guc!r}."
                        ),
                        id="django_rls_tenants.W001",
                    )
                )
            if constraint.guc_admin_var != runtime_admin_guc:
                warnings.append(
                    CheckWarning(
                        f"RLSConstraint on {model.__name__} uses "
                        f"guc_admin_var={constraint.guc_admin_var!r} but "
                        f"GUC_PREFIX derives {runtime_admin_guc!r}. "
                        f"Admin bypass will silently break.",
                        hint=(
                            f"Either pass guc_admin_var={runtime_admin_guc!r} to "
                            f"the RLSConstraint, or set RLS_TENANTS['GUC_PREFIX'] so "
                            f"it derives {runtime_admin_guc!r}."
                        ),
                        id="django_rls_tenants.W002",
                    )
                )
    return warnings


def _check_conn_max_age_with_session_gucs() -> list[CheckWarning]:
    """Warn if ``CONN_MAX_AGE > 0`` with ``USE_LOCAL_SET=False``.

    Checks all aliases in ``RLS_TENANTS["DATABASES"]``. Persistent
    connections with session-scoped GUCs risk stale tenant context
    leaking between requests if cleanup fails.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if rls_tenants_config.USE_LOCAL_SET:
        return warnings

    django_databases = getattr(settings, "DATABASES", {})
    for alias in rls_tenants_config.DATABASES:
        db_config = django_databases.get(alias, {})
        conn_max_age = db_config.get("CONN_MAX_AGE", 0)
        # None means "keep connections forever" -- the most dangerous value.
        if conn_max_age is None or (isinstance(conn_max_age, (int, float)) and conn_max_age > 0):
            warnings.append(
                CheckWarning(
                    f"DATABASES[{alias!r}] has CONN_MAX_AGE={conn_max_age} "
                    f"with USE_LOCAL_SET=False. This risks GUC state leaking "
                    f"between requests on persistent connections. Consider "
                    f"USE_LOCAL_SET=True, which scopes the GUCs to "
                    f"transactions, so they never stay on a connection.",
                    hint=f"Set RLS_TENANTS['USE_LOCAL_SET'] = True, "
                    f"or set DATABASES[{alias!r}]['CONN_MAX_AGE'] = 0.",
                    id="django_rls_tenants.W004",
                )
            )
    return warnings


def _check_pool_without_reset() -> list[CheckWarning]:
    """Warn if a pooled alias has no ``reset`` callback with ``USE_LOCAL_SET=False``.

    Checks all aliases in ``RLS_TENANTS["DATABASES"]``. Django rejects a
    non-zero ``CONN_MAX_AGE`` with ``OPTIONS["pool"]``, so ``W004`` never fires
    for a working pool, yet a pooled connection keeps its session GUCs across
    requests just like a persistent one. The ``connection_created`` handler
    clears them on the next checkout; a ``reset`` callback also clears them on
    the way back into the pool, so idle connections hold no tenant state.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.pool import _pool_options  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    if rls_tenants_config.USE_LOCAL_SET:
        return warnings

    django_databases = getattr(settings, "DATABASES", {})
    for alias in rls_tenants_config.DATABASES:
        pool_options = _pool_options(django_databases.get(alias, {}))
        if pool_options is not None and pool_options.get("reset") is None:
            warnings.append(
                CheckWarning(
                    f"DATABASES[{alias!r}] uses Django's connection pool with "
                    f"USE_LOCAL_SET=False and no pool 'reset' callback. A connection "
                    f"that a request's cleanup did not reach goes back to the pool "
                    f"still holding that request's RLS GUCs, until its next checkout "
                    f"clears them.",
                    hint=(
                        f"Set DATABASES[{alias!r}]['OPTIONS']['pool']['reset'] to "
                        f"django_rls_tenants.pool.reset_rls_gucs, or set "
                        f"RLS_TENANTS['USE_LOCAL_SET'] = True."
                    ),
                    id="django_rls_tenants.W010",
                )
            )
    return warnings


def _check_databases_alias_exists() -> list[CheckWarning]:
    """Warn if ``DATABASES`` contains an alias not in ``settings.DATABASES``.

    Catches typos like ``"replca"`` instead of ``"replica"`` that would
    cause ``set_guc`` to fail at runtime when the middleware tries to
    open a connection to a non-existent database alias.
    """
    from django.conf import settings  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    django_databases = set(getattr(settings, "DATABASES", {}).keys())
    return [
        CheckWarning(
            f"RLS_TENANTS['DATABASES'] contains {alias!r} which is "
            f"not defined in settings.DATABASES. The middleware will "
            f"fail at runtime when trying to set GUCs on this alias.",
            hint=(
                f"Check for typos. Defined database aliases: "
                f"{', '.join(sorted(django_databases))}."
            ),
            id="django_rls_tenants.W006",
        )
        for alias in rls_tenants_config.DATABASES
        if alias not in django_databases
    ]


def _check_tenant_model_resolves() -> list[CheckWarning]:
    """Warn if ``TENANT_MODEL`` does not resolve to an installed model.

    ``RLS_TENANTS["TENANT_MODEL"]`` must be a ``"<app_label>.<ModelName>"``
    path to a model in ``INSTALLED_APPS``. A typo or an app missing from
    ``INSTALLED_APPS`` otherwise surfaces only at query time -- when the
    tenant FK is resolved -- with a cryptic ``LookupError``.
    """
    from django.apps import apps  # noqa: PLC0415

    from django_rls_tenants.exceptions import RLSConfigurationError  # noqa: PLC0415
    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415

    warnings: list[CheckWarning] = []
    try:
        model_path = rls_tenants_config.TENANT_MODEL
    except RLSConfigurationError:
        # TENANT_MODEL is required; its absence is reported when the config
        # is first read elsewhere. Don't crash the check here.
        return warnings

    try:
        apps.get_model(model_path)
    except (LookupError, ValueError):
        # ValueError: malformed path (not "app_label.ModelName").
        # LookupError: unknown app label or model name.
        warnings.append(
            CheckWarning(
                f"RLS_TENANTS['TENANT_MODEL'] = {model_path!r} does not "
                f"resolve to an installed model.",
                hint=(
                    "Set RLS_TENANTS['TENANT_MODEL'] to '<app_label>.<ModelName>' "
                    "naming a model in INSTALLED_APPS."
                ),
                id="django_rls_tenants.W008",
            )
        )
    return warnings


def _check_tenant_fk_field_exists() -> list[CheckWarning]:
    """Warn if a concrete ``RLSProtectedModel`` subclass lacks its tenant field.

    Each model's ``RLSConstraint`` generates an RLS policy that filters on a
    tenant column. The referenced field name comes from the model's
    ``RLSConstraint(field=...)`` when present, falling back to
    ``RLS_TENANTS["TENANT_FK_FIELD"]`` (default ``"tenant"``) -- the same
    resolution order ``register_m2m_rls`` uses.

    If that field is missing -- e.g. a constraint's ``field`` (or
    ``TENANT_FK_FIELD``) names a column no model actually defines -- the
    generated policy references a column that doesn't exist, failing at
    migrate/query time. This catches the mismatch at startup.

    The field name is resolved from the constraint rather than blindly from
    ``TENANT_FK_FIELD``: the ``class_prepared`` injector always adds a field
    named ``TENANT_FK_FIELD``, so checking that name alone would always pass.
    The policy uses the *constraint's* field, so that is what must exist.
    """
    from django.apps import apps  # noqa: PLC0415
    from django.core.exceptions import FieldDoesNotExist  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.models import (  # noqa: PLC0415
        RLSProtectedModel,
        _get_tenant_fk_field,
    )

    warnings: list[CheckWarning] = []
    default_fk_field = rls_tenants_config.TENANT_FK_FIELD
    for model in apps.get_models():
        if not issubclass(model, RLSProtectedModel) or model._meta.abstract:  # noqa: SLF001
            continue
        fk_field = _get_tenant_fk_field(model) or default_fk_field
        try:
            model._meta.get_field(fk_field)  # noqa: SLF001
        except FieldDoesNotExist:
            warnings.append(
                CheckWarning(
                    f"{model.__name__} has no field {fk_field!r} referenced by its RLS policy.",
                    hint=(
                        f"Add a {fk_field!r} ForeignKey to {model.__name__}, or "
                        f"point RLS_TENANTS['TENANT_FK_FIELD'] (or the constraint's "
                        f"field=) at an existing field."
                    ),
                    id="django_rls_tenants.W009",
                    obj=model,
                )
            )
    return warnings


class _UniqueSet(NamedTuple):
    """A set of columns PostgreSQL keeps unique across one model's table."""

    description: str
    fields: tuple[str, ...]  # resolved names, in declared order
    name: str  # the field or constraint name; empty for a ``unique_together`` entry
    by_expressions: bool


def _check_unique_across_tenants() -> list[CheckWarning]:
    """Warn about unique fields and constraints that omit the tenant field (``W012``).

    PostgreSQL checks unique indexes outside the row-security policies, so a
    unique index on an RLS-protected table spans every tenant: a write whose
    value exists only in another tenant fails with an ``IntegrityError``, which
    tells the writer that the value exists (a company registration number, a
    slug). Walks every concrete model with an ``RLSConstraint`` and reports each
    ``unique=True`` field other than the primary key, ``unique_together`` entry
    and ``UniqueConstraint`` that does not include the tenant field (the
    ``RLSConstraint``'s ``field``). A ``UniqueConstraint`` on expressions
    includes it when one of them references the field.

    Uniqueness across tenants that is intended is listed in
    ``RLS_TENANTS["UNIQUE_ACROSS_TENANTS"]``; an entry there that matches no
    reported unique set (a stale label or name) is reported too. A model whose
    tenant field does not exist is left to ``W009``.
    """
    from django.apps import apps  # noqa: PLC0415
    from django.core.exceptions import FieldDoesNotExist  # noqa: PLC0415

    from django_rls_tenants.tenants.conf import rls_tenants_config  # noqa: PLC0415
    from django_rls_tenants.tenants.models import _get_tenant_fk_field  # noqa: PLC0415

    allowlist = rls_tenants_config.UNIQUE_ACROSS_TENANTS
    # Labels are case-insensitive, so two spellings of one label list the same model.
    entries_by_label: dict[str, list[str | tuple[str, ...]]] = {}
    for label, entries in allowlist.items():
        entries_by_label.setdefault(label.lower(), []).extend(entries)
    warnings: list[CheckWarning] = []
    checked: set[str] = set()
    for model in apps.get_models():
        fk_field = _get_tenant_fk_field(model)
        if fk_field is None or model._meta.proxy:  # noqa: SLF001
            continue
        label = model._meta.label_lower  # noqa: SLF001
        checked.add(label)
        try:
            tenant_field = model._meta.get_field(fk_field).name  # noqa: SLF001
        except FieldDoesNotExist:
            continue
        warnings.extend(
            _unique_across_tenants_warnings(
                model,
                tenant_field,
                entries_by_label.get(label, []),
            )
        )
    warnings.extend(
        CheckWarning(
            f"RLS_TENANTS['UNIQUE_ACROSS_TENANTS'] lists {label!r}, which names no "
            f"installed, non-proxy model with an RLSConstraint.",
            hint=(
                "Remove the entry, or correct the label to '<app_label>.<ModelName>' "
                "of an RLS-protected model."
            ),
            id="django_rls_tenants.W012",
        )
        for label in allowlist
        if label.lower() not in checked
    )
    return warnings


def _unique_across_tenants_warnings(
    model: type[models.Model],
    tenant_field: str,
    entries: list[str | tuple[str, ...]],
) -> list[CheckWarning]:
    """Build ``W012`` for each unique set of ``model`` that omits ``tenant_field``.

    ``entries`` is the model's ``UNIQUE_ACROSS_TENANTS`` list; each entry that
    matches none of the reported sets gets a ``W012`` of its own.
    """
    resolve = _field_name_resolver(model)
    label = model._meta.label  # noqa: SLF001
    warnings: list[CheckWarning] = []
    used: set[str | tuple[str, ...]] = set()
    for unique_set in _unique_sets(model):
        if tenant_field in unique_set.fields:
            continue
        matched = [entry for entry in entries if _allowlists(entry, unique_set, resolve)]
        if matched:
            used.update(matched)
            continue
        if unique_set.by_expressions:
            suggestion: str | tuple[str, ...] = unique_set.name
            fix = f"reference {tenant_field!r} in the constraint's expressions"
        else:
            suggestion = unique_set.fields[0] if len(unique_set.fields) == 1 else unique_set.fields
            fields = ", ".join(repr(name) for name in (tenant_field, *unique_set.fields))
            fix = f"declare models.UniqueConstraint(fields=[{fields}], name=...) in its place"
        warnings.append(
            CheckWarning(
                f"{unique_set.description} is unique across all tenants: it does not "
                f"include the tenant field {tenant_field!r}. PostgreSQL checks unique "
                f"indexes outside the RLS policies, so a write whose value exists only "
                f"in another tenant fails with an IntegrityError, telling the writer "
                f"that the value exists there.",
                hint=(
                    f"Scope it to the tenant: {fix}. If uniqueness across tenants is "
                    f"intended, list it in RLS_TENANTS['UNIQUE_ACROSS_TENANTS'] = "
                    f"{{{label!r}: [{suggestion!r}]}}."
                ),
                id="django_rls_tenants.W012",
                obj=model,
            )
        )
    warnings.extend(
        CheckWarning(
            f"RLS_TENANTS['UNIQUE_ACROSS_TENANTS'][{label!r}] lists {entry!r}, which "
            f"matches no unique field or constraint of {label} that omits the tenant "
            f"field {tenant_field!r}.",
            hint=(
                "Remove the entry, or correct it to a unique field's name, a "
                "UniqueConstraint's name, or the tuple of a unique set's field names."
            ),
            id="django_rls_tenants.W012",
            obj=model,
        )
        for entry in entries
        if entry not in used
    )
    return warnings


def _field_name_resolver(model: type[models.Model]) -> Callable[[str], str]:
    """Return a function mapping a field name or attname (``tenant_id``) to the name."""
    from django.core.exceptions import FieldDoesNotExist  # noqa: PLC0415

    def resolve(name: str) -> str:
        try:
            return model._meta.get_field(name).name  # noqa: SLF001
        except FieldDoesNotExist:
            return name  # Django's own model checks report the unknown name

    return resolve


def _unique_sets(model: type[models.Model]) -> Iterator[_UniqueSet]:
    """Yield every set of columns ``model`` declares unique, except its primary key.

    Only the model's own fields: a multi-table-inheritance parent's unique
    fields live in the parent's table and are reported on the parent.
    """
    from django.db.models import UniqueConstraint  # noqa: PLC0415

    opts = model._meta  # noqa: SLF001
    resolve = _field_name_resolver(model)
    for field in opts.local_concrete_fields:
        if field.unique and not field.primary_key:
            yield _UniqueSet(
                f"Field {field.name!r} (unique=True)",
                (field.name,),
                field.name,
                by_expressions=False,
            )
    for together in opts.unique_together:
        yield _UniqueSet(
            f"unique_together entry {tuple(together)!r}",
            tuple(resolve(name) for name in together),
            "",
            by_expressions=False,
        )
    for constraint in opts.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        if constraint.fields:
            yield _UniqueSet(
                f"UniqueConstraint {constraint.name!r} on {tuple(constraint.fields)!r}",
                tuple(resolve(name) for name in constraint.fields),
                constraint.name,
                by_expressions=False,
            )
        else:
            yield _UniqueSet(
                f"UniqueConstraint {constraint.name!r} on expressions",
                tuple(
                    resolve(name)
                    for expression in constraint.expressions
                    for name in _referenced_field_names(expression)
                ),
                constraint.name,
                by_expressions=True,
            )


def _referenced_field_names(expression: Any) -> Iterator[str]:
    """Yield the field each ``F()`` in ``expression`` references (``a__b`` gives ``a``)."""
    from django.db.models import F  # noqa: PLC0415
    from django.db.models.constants import LOOKUP_SEP  # noqa: PLC0415

    # A bare ``F()`` has no ``flatten()``; an expression's yields every node, ``F()``s included.
    nodes: Iterable[Any] = (
        [expression] if isinstance(expression, F) else getattr(expression, "flatten", tuple)()
    )
    for node in nodes:
        if isinstance(node, F):
            name: str = node.name  # type: ignore[attr-defined]  # set in F.__init__, not in the stubs
            yield name.split(LOOKUP_SEP, 1)[0]


def _allowlists(
    entry: str | tuple[str, ...],
    unique_set: _UniqueSet,
    resolve: Callable[[str], str],
) -> bool:
    """Whether ``UNIQUE_ACROSS_TENANTS`` ``entry`` names ``unique_set``.

    A string names a ``UniqueConstraint`` by its name, or a unique set of that
    one field; a tuple names a unique set of exactly those fields. Field sets
    only match sets declared by field names, not by expressions.
    """
    if isinstance(entry, str):
        if unique_set.name and entry == unique_set.name:
            return True
        entry = (entry,)
    return not unique_set.by_expressions and (
        {resolve(name) for name in entry} == set(unique_set.fields)
    )
