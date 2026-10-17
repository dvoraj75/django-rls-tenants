"""pytest plugin: run tests as a PostgreSQL role that Row-Level Security applies to.

A superuser, or a role with ``BYPASSRLS``, bypasses every RLS policy, even with
``FORCE ROW LEVEL SECURITY``. Development and test databases often connect as one, and then
a test suite proves nothing about tenant isolation while it stays green. The fixtures here
switch such a connection to a plain role with ``SET ROLE`` for the test and back with
``RESET ROLE``; on a connection RLS already applies to they do nothing.

The plugin is registered through the ``pytest11`` entry point ``django_rls_tenants``, so it
loads in every pytest run where the package is installed (``-p no:django_rls_tenants`` turns
it off). pytest imports it at startup, before Django settings are configured, so this module
imports nothing from Django (or from the rest of the package) at module level. The fixtures
need pytest-django.

Two ini options configure it:

- ``rls_role``: the role to switch to (default ``rls_test_role``), created ``NOLOGIN`` once
  per session if missing.
- ``rls_role_aliases``: the database aliases to switch (default ``default``).
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Literal, Protocol

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from django.db.backends.base.base import BaseDatabaseWrapper

_DEFAULT_ROLE = "rls_test_role"

# An unquoted PostgreSQL identifier (at most 63 bytes), so it can go into SQL and into
# libpq's ``-c role=...`` as written and matches ``pg_roles.rolname`` without quoting.
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class _DjangoDbBlocker(Protocol):
    """The part of pytest-django's ``django_db_blocker`` the plugin uses."""

    def unblock(self) -> AbstractContextManager[None]:
        """Allow database access in the block."""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``rls_role`` and ``rls_role_aliases`` ini options."""
    parser.addini(
        "rls_role",
        help="django-rls-tenants: role the enforce_rls fixture switches to (SET ROLE)",
        default=_DEFAULT_ROLE,
    )
    parser.addini(
        "rls_role_aliases",
        type="args",
        help="django-rls-tenants: database aliases the enforce_rls fixtures switch",
        default=["default"],
    )


def pytest_configure(config: pytest.Config) -> None:
    """Reject an ``rls_role`` or ``rls_role_aliases`` value the fixtures cannot use.

    Raises:
        pytest.UsageError: If ``rls_role`` is not a lowercase unquoted identifier, or
            ``rls_role_aliases`` is empty.
    """
    role = config.getini("rls_role")
    if not _ROLE_NAME.fullmatch(role):
        raise pytest.UsageError(
            f"django-rls-tenants: rls_role = {role!r} is not a valid role name. Use a "
            f"lowercase PostgreSQL identifier of at most 63 characters (letters, digits "
            f"and underscores, not starting with a digit), such as {_DEFAULT_ROLE!r}."
        )
    if not _aliases(config):
        raise pytest.UsageError(
            "django-rls-tenants: rls_role_aliases is empty. List the database aliases the "
            "enforce_rls fixtures switch (default: default), or turn the plugin off with "
            "-p no:django_rls_tenants."
        )


def _aliases(config: pytest.Config) -> list[str]:
    """Return the configured ``rls_role_aliases``, without duplicates, in order."""
    aliases: list[str] = config.getini("rls_role_aliases")
    return list(dict.fromkeys(aliases))


def _bypasses_rls(
    conn: BaseDatabaseWrapper,
    *,
    role: Literal["current_user", "session_user"] = "current_user",
) -> bool:
    """Return whether ``conn``'s role bypasses RLS (superuser or ``BYPASSRLS``).

    Args:
        conn: The connection to ask.
        role: ``"session_user"`` asks about the login role, which ``SET ROLE`` does not
            change.
    """
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = {role}")
        row = cursor.fetchone()
    return bool(row and row[0])


def _prepare_role(conn: BaseDatabaseWrapper, role: str) -> None:
    """Create ``role`` if missing and grant it what Django's tests need on ``conn``'s database.

    Does nothing if ``conn``'s role is already subject to RLS: ``enforce_rls`` never
    switches such a connection, and such a role usually lacks ``CREATEROLE``. Otherwise
    ``role`` gets ``USAGE`` on every schema of the ``search_path``, ``ALL`` on their tables
    (``django_migrations`` included) and sequences, and the same on tables and sequences
    the connection's role creates in them later (``ALTER DEFAULT PRIVILEGES``).

    Raises:
        pytest.fail.Exception: If ``role`` exists and is a superuser or has ``BYPASSRLS``.
    """
    from django.db import DatabaseError, transaction  # noqa: PLC0415  -- see module docstring

    if not _bypasses_rls(conn):
        return
    with conn.cursor() as cursor:
        cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = %s", [role])
        row = cursor.fetchone()
        if row is None:
            try:
                with transaction.atomic(using=conn.alias):
                    cursor.execute(f"CREATE ROLE {role} NOLOGIN")
            except DatabaseError:
                # Another session (a pytest-xdist worker) created it meanwhile.
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
                if cursor.fetchone() is None:
                    raise
        elif row[0]:
            pytest.fail(
                f"django-rls-tenants: the RLS test role {role!r} is a superuser or has "
                f"BYPASSRLS, so RLS would still be bypassed after SET ROLE {role}. Use a "
                f"plain role: ALTER ROLE {role} NOSUPERUSER NOBYPASSRLS, or set another "
                f"name with the rls_role ini option.",
                pytrace=False,
            )
        cursor.execute("SELECT quote_ident(name) FROM unnest(current_schemas(false)) AS name")
        schemas = [name for (name,) in cursor.fetchall()]
        for schema in schemas:
            cursor.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
            for kind in ("TABLES", "SEQUENCES"):
                cursor.execute(f"GRANT ALL ON ALL {kind} IN SCHEMA {schema} TO {role}")
                cursor.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT ALL ON {kind} TO {role}"
                )


def _switch_role(conn: BaseDatabaseWrapper, role: str) -> bool:
    """Run ``conn`` as ``role`` if its role bypasses RLS.

    Returns:
        ``True`` if the role was switched (the caller resets it), ``False`` if the
        connection's role is already subject to RLS.
    """
    bypasses = _bypasses_rls(conn)
    if bypasses:
        with conn.cursor() as cursor:
            cursor.execute(f"SET ROLE {role}")
    return bypasses


def _in_failed_transaction(conn: BaseDatabaseWrapper) -> bool:
    """Whether ``conn``'s open DB-API connection sits in an aborted transaction."""
    from django_rls_tenants.tenants.affinity import _IN_ERROR, _status_reader  # noqa: PLC0415

    return _status_reader(conn.connection)() == _IN_ERROR


def _fail_inside_a_test_transaction(conn: BaseDatabaseWrapper) -> None:
    """Fail when ``rls_role`` would create the role inside a test transaction.

    pytest sets session fixtures up before function ones, so this happens only when
    ``rls_role`` is first requested from inside a test (``request.getfixturevalue()``) that
    runs in a transaction. The rollback would drop the role and every later ``enforce_rls``
    would fail with "role does not exist".

    Raises:
        pytest.fail.Exception: If ``conn`` is inside ``transaction.atomic()``.
    """
    if conn.in_atomic_block:
        pytest.fail(
            f"django-rls-tenants: rls_role was first requested inside a transaction on the "
            f"{conn.alias!r} connection (request.getfixturevalue() in a test that is not "
            f"django_db(transaction=True)?), so the CREATE ROLE and GRANTs it runs would be "
            f"rolled back with the test, and later tests using enforce_rls would fail. "
            f"Request rls_role or enforce_rls as an argument of the test or fixture instead, "
            f"so it is set up before the test transaction opens.",
            pytrace=False,
        )


def _switch_role_as_opened(
    aliases: set[str],
    role: str,
    *,
    on_this_thread: bool,
    switched: list[BaseDatabaseWrapper] | None = None,
) -> Callable[..., None]:
    """Return a ``connection_created`` receiver that switches connections as they open.

    Args:
        aliases: Switch connections on these aliases only.
        role: The role to switch to.
        on_this_thread: ``True`` switches connections opened on the thread that built the
            receiver (one the test closed and reopened), ``False`` those opened on every
            other thread.
        switched: If given, the wrappers the receiver switched are added to it (once each),
            for the caller to reset.
    """
    this_thread = threading.get_ident()

    def switch_role(
        sender: type,  # noqa: ARG001  -- Django signal API
        connection: BaseDatabaseWrapper,
        **kwargs: object,  # noqa: ARG001  -- Django signal API
    ) -> None:
        if (threading.get_ident() == this_thread) is not on_this_thread:
            return
        if connection.alias not in aliases or not _switch_role(connection, role):
            return
        if switched is not None and connection not in switched:
            switched.append(connection)

    return switch_role


@pytest.fixture(scope="session")
def rls_role(
    request: pytest.FixtureRequest,
    django_db_setup: None,  # noqa: ARG001  -- the test databases must exist first
    django_db_blocker: _DjangoDbBlocker,
) -> str:
    """The name of the plain role ``enforce_rls`` switches to (ini option ``rls_role``).

    Once per session, on each alias in ``rls_role_aliases`` whose connection bypasses RLS,
    creates the role ``NOLOGIN`` if it is missing and grants it ``USAGE`` on the schemas of
    the ``search_path``, ``ALL`` on their tables and sequences, and the same on tables and
    sequences created there later. Where the connection is already subject to RLS nothing
    is created. Request it (or ``enforce_rls``) as a fixture argument, so that it is set up
    before the test transaction opens: first requested with ``request.getfixturevalue()``
    inside a transaction, where the rollback would drop the role, it fails.
    """
    from django.db import connections  # noqa: PLC0415  -- see module docstring

    role: str = request.config.getini("rls_role")
    with django_db_blocker.unblock():
        for alias in _aliases(request.config):
            conn = connections[alias]
            if conn.in_atomic_block and _bypasses_rls(conn):
                _fail_inside_a_test_transaction(conn)
            _prepare_role(conn, role)
    return role


@pytest.fixture
def enforce_rls(
    request: pytest.FixtureRequest,
    rls_role: str,
    db: None,  # noqa: ARG001  -- database access for the test
) -> Iterator[None]:
    """Run the test with PostgreSQL RLS enforced on its connections.

    Switches each alias in ``rls_role_aliases`` whose connection bypasses RLS (superuser or
    ``BYPASSRLS``) to ``rls_role`` with ``SET ROLE``, and resets it after the test; on a
    plain role it does nothing. Aliases the test may not use (pytest-django's
    ``django_db(databases=...)``) are left alone. Request it in every test that asserts
    database-level row filtering: on a superuser connection such a test passes whether or
    not the policies work. A connection the test closes and reopens is switched again as
    it opens. Only the test thread's connections are switched; see
    ``enforce_rls_on_other_threads``.
    """
    from django.db import connections  # noqa: PLC0415  -- see module docstring
    from django.db.backends.signals import connection_created  # noqa: PLC0415

    # Not in django-stubs; raised for an alias the test may not use.
    from django.test.testcases import (  # type: ignore[attr-defined]  # noqa: PLC0415
        DatabaseOperationForbidden,
    )

    aliases = _aliases(request.config)
    switched: list[BaseDatabaseWrapper] = []
    switch_reopened = _switch_role_as_opened(
        set(aliases), rls_role, on_this_thread=True, switched=switched
    )
    try:
        for alias in aliases:
            conn = connections[alias]
            try:
                if _switch_role(conn, rls_role):
                    switched.append(conn)
            except DatabaseOperationForbidden:
                continue
        connection_created.connect(switch_reopened, weak=False)
        yield
    finally:
        connection_created.disconnect(switch_reopened)
        for conn in switched:
            # A closed connection took its SET ROLE with it (a reopened one was switched
            # again as it opened, and is reset here). In an aborted transaction RESET ROLE
            # would only raise, and would hide the test's own error: the rollback that
            # ends the transaction reverts the SET ROLE issued inside it.
            if conn.connection is None or conn.closed_in_transaction:
                continue
            if _in_failed_transaction(conn):
                continue
            with conn.cursor() as cursor:
                cursor.execute("RESET ROLE")


@pytest.fixture
def enforce_rls_on_other_threads(request: pytest.FixtureRequest, rls_role: str) -> Iterator[None]:
    """Run connections opened on other threads as ``rls_role``.

    ``enforce_rls`` switches the test thread's connections only. Under ASGI, Django runs some
    work (error pages, ``thread_sensitive=False`` hops) on worker threads with connections
    of their own; this fixture switches each one on an alias in ``rls_role_aliases`` as it
    opens. Such connections see committed rows only, so it suits
    ``django_db(transaction=True)`` tests. Its ``connection_created`` receiver is connected
    after the library's handler, so it runs after that handler has applied or cleared the
    GUCs.
    """
    from django.db.backends.signals import connection_created  # noqa: PLC0415

    switch_role = _switch_role_as_opened(
        set(_aliases(request.config)), rls_role, on_this_thread=False
    )
    connection_created.connect(switch_role, weak=False)
    try:
        yield
    finally:
        connection_created.disconnect(switch_role)
