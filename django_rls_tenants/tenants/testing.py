"""Test helpers for verifying RLS behavior.

Provides context managers for test setup (``rls_bypass``, ``rls_as_tenant``)
and assertion functions for verifying RLS policies are applied correctly
(``assert_rls_enforced`` checks the connection's role itself), plus their
counterparts for ``async def`` tests (``async_rls_bypass``,
``async_rls_as_tenant``, ``aassert_rls_enforced``, ``aassert_rls_enabled``,
``aassert_rls_policy_exists`` and ``aassert_rls_blocks_without_context``).
The ``enforce_rls`` fixture lives in the pytest plugin,
``django_rls_tenants.pytest_plugin``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import connections

from django_rls_tenants.rls.context import rls_context
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import (
    _writes_gucs,
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
)
from django_rls_tenants.tenants.state import _reset_context_gucs, _set_context_gucs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from django.db import models


@contextmanager
def rls_bypass(*, using: str = "default") -> Iterator[None]:
    """Temporarily enable admin bypass for tests.

    Args:
        using: Database alias. Default: ``"default"``.

    Usage::

        with rls_bypass():
            all_orders = Order.objects.all()  # sees all tenants
    """
    with admin_context(using=using):
        yield


@contextmanager
def rls_as_tenant(tenant_id: int | str, *, using: str = "default") -> Iterator[None]:
    """Scope to a specific tenant for tests.

    Args:
        tenant_id: The tenant PK to scope to.
        using: Database alias. Default: ``"default"``.

    Usage::

        with rls_as_tenant(tenant_id=42):
            orders = Order.objects.all()  # only tenant 42
    """
    with tenant_context(tenant_id, using=using):
        yield


def assert_rls_enforced(*, using: str = "default") -> None:
    """Assert that PostgreSQL applies RLS to the ``using`` connection's current role.

    A superuser, or a role with ``BYPASSRLS``, bypasses every RLS policy, even
    with ``FORCE ROW LEVEL SECURITY``, so every isolation test on such a
    connection passes whether or not the policies work. The check reads
    ``current_user``, the role ``SET ROLE`` switches to (the ``enforce_rls``
    fixture), not the login role.

    Args:
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If the connection's current role is a superuser or has
            ``BYPASSRLS``.
    """
    conn = connections[using]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone()
    assert row is not None, f"The current role of the {using!r} connection is not in pg_roles"
    role, is_superuser, bypasses_rls = row
    reasons = [
        reason
        for reason, applies in (
            ("is a PostgreSQL superuser", is_superuser),
            ("has the BYPASSRLS attribute", bypasses_rls),
        )
        if applies
    ]
    assert not reasons, (
        f"The {using!r} connection runs as role {role!r}, which {' and '.join(reasons)}, "
        f"so it bypasses every RLS policy and tests on it prove nothing about tenant "
        f"isolation. Connect as a role with NOSUPERUSER and NOBYPASSRLS, or request the "
        f"enforce_rls pytest fixture, which switches to such a role with SET ROLE."
    )


def assert_rls_enabled(table_name: str, *, using: str = "default") -> None:
    """Assert that RLS is enabled and forced on the given table.

    Args:
        table_name: The database table name to check.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If RLS is not enabled or not forced.
    """
    conn = connections[using]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            [table_name],
        )
        row = cursor.fetchone()
        assert row is not None, f"Table '{table_name}' does not exist"
        assert row[0] is True, f"RLS is not enabled on table '{table_name}'"
        assert row[1] is True, f"RLS is not forced on table '{table_name}'"


def assert_rls_policy_exists(
    table_name: str,
    policy_name: str | None = None,
    *,
    using: str = "default",
) -> None:
    """Assert that an RLS policy exists on the given table.

    Args:
        table_name: The database table name to check.
        policy_name: Expected policy name. Defaults to
            ``"{table_name}_tenant_isolation_policy"``.
        using: Database alias. Default: ``"default"``.
    """
    if policy_name is None:
        policy_name = f"{table_name}_tenant_isolation_policy"

    conn = connections[using]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_policies WHERE tablename = %s AND policyname = %s",
            [table_name, policy_name],
        )
        assert cursor.fetchone() is not None, (
            f"RLS policy '{policy_name}' does not exist on table '{table_name}'"
        )


@contextmanager
def _no_context_for_the_guard() -> Iterator[None]:
    """Make the GUC affinity guard apply no context in the block, whatever encloses it."""
    entered = _set_context_gucs(None)
    try:
        yield
    finally:
        _reset_context_gucs(entered)


def _count_rows(model_class: type[models.Model], *, using: str) -> int:
    """Count the rows of ``model_class``'s table that the ``using`` connection can see.

    Raw SQL, so the result depends only on the table's RLS policy, the
    connection's role and its GUCs: the ``STRICT_MODE`` guard, the ORM
    auto-scope filter and any filter of the model's default manager are not
    involved.

    Args:
        model_class: The model whose ``_meta.db_table`` is counted.
        using: Database alias.

    Returns:
        The number of rows the connection can see.
    """
    conn = connections[using]
    table = conn.ops.quote_name(model_class._meta.db_table)  # noqa: SLF001  -- standard Django _meta access
    with conn.cursor() as cursor:
        # Developer-controlled name from the model's Meta, quoted by the backend.
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count: int = cursor.fetchone()[0]
    return count


def assert_rls_blocks_without_context(
    model_class: type[models.Model],
    *,
    using: str = "default",
) -> None:
    """Assert that the database returns zero rows of ``model_class`` with no RLS context.

    Verifies the fail-closed behavior of the table's RLS policy. Requires at
    least one row in the table (the caller must set up test data first).

    1. Inside :func:`~django_rls_tenants.tenants.context.admin_context`, the
       table must hold at least one row, so the assertion cannot pass
       vacuously.
    2. With the admin and tenant GUCs cleared on the ``using`` connection, the
       table must return zero rows. The GUCs are restored before the
       assertion, so the result does not depend on the caller's admin or
       tenant context, and such a context (for example :func:`rls_bypass` or
       :func:`rls_as_tenant`) is intact when the helper returns or fails.
       With ``USE_LOCAL_SET=True`` on an alias in
       ``RLS_TENANTS["DATABASES"]``, where such a context lives in the
       ``ContextVar`` values only and the GUC affinity guard applies it to
       each statement, the count runs without it, and the GUCs are cleared
       and restored for the session. On any other alias that mode uses
       ``SET LOCAL``.
       Other GUCs, such as a flag set with ``bypass_flag()``, are left as they
       are.

    Both counts are raw SQL on ``model_class._meta.db_table``, so the
    ``STRICT_MODE`` guard, the ORM auto-scope filter and the filters of the
    model's default manager are not involved: the helper works with
    ``STRICT_MODE=True``, and every row in the table must be hidden.

    Args:
        model_class: The RLS-protected model class to query.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If any rows are returned, or if the table is empty
            (which would make the assertion pass vacuously).
        RuntimeError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and its connection is not inside
            ``transaction.atomic()``.
    """
    conf = rls_tenants_config

    # Pre-check: verify the table has data (via admin bypass) so the
    # assertion is not vacuously true on an empty table.
    with admin_context(using=using):
        total = _count_rows(model_class, using=using)
    assert total > 0, (
        f"assert_rls_blocks_without_context requires at least one row in "
        f"{model_class.__name__}, but the table is empty. "
        f"Set up test data before calling this helper."
    )

    # Clear the admin and tenant GUCs for the count only, so a context the
    # caller holds cannot make rows visible, then restore them.
    no_context = {conf.GUC_IS_ADMIN: "", conf.GUC_CURRENT_TENANT: ""}
    if _writes_gucs(using, is_local=conf.USE_LOCAL_SET):
        with rls_context(no_context, is_local=conf.USE_LOCAL_SET, using=using):
            count = _count_rows(model_class, using=using)
    else:
        # USE_LOCAL_SET=True on a guarded alias: the guard would apply the caller's
        # context before the count.
        with _no_context_for_the_guard(), rls_context(no_context, using=using):
            count = _count_rows(model_class, using=using)
    table = model_class._meta.db_table  # noqa: SLF001  -- standard Django _meta access
    assert count == 0, (
        f"Expected 0 rows from {model_class.__name__} (table '{table}') without RLS "
        f"context, got {count}, so RLS may not be enforced. Check that the connection "
        f"role is not a superuser and has no BYPASSRLS (system check W005). Check that "
        f"RLS is enabled and forced on the table and that it has a policy "
        f"(assert_rls_enabled(), assert_rls_policy_exists()). Check that no bypass flag "
        f"is set."
    )


@asynccontextmanager
async def async_rls_bypass(*, using: str = "default") -> AsyncIterator[None]:
    """Async counterpart of :func:`rls_bypass`, for ``async with`` in async tests.

    Wraps :func:`~django_rls_tenants.tenants.context.async_admin_context`: one
    ``thread_sensitive`` hop on entry and one on exit, with the admin state set
    in the test coroutine itself, so the async ORM sees it. The hops reach the
    test's own connection and transaction only when the coroutine is driven by
    ``async_to_sync``, as Django's ``TestCase`` does for ``async def`` test
    methods (see the testing guide, "Async Tests").

    Args:
        using: Database alias. Default: ``"default"``.

    Raises:
        RLSConfigurationError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and no transaction is open on the hop
            thread.

    Usage::

        async with async_rls_bypass():
            total = await Order.objects.acount()  # sees all tenants
    """
    async with async_admin_context(using=using):
        yield


@asynccontextmanager
async def async_rls_as_tenant(
    tenant_id: int | str,
    *,
    using: str = "default",
) -> AsyncIterator[None]:
    """Async counterpart of :func:`rls_as_tenant`, for ``async with`` in async tests.

    Wraps :func:`~django_rls_tenants.tenants.context.async_tenant_context`,
    with the same hop model as :func:`async_rls_bypass`.

    Args:
        tenant_id: The tenant PK to scope to.
        using: Database alias. Default: ``"default"``.

    Raises:
        NoTenantContextError: If ``tenant_id`` is ``None``.
        RLSConfigurationError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and no transaction is open on the hop
            thread.

    Usage::

        async with async_rls_as_tenant(tenant_id=42):
            orders = [order async for order in Order.objects.all()]  # only tenant 42
    """
    async with async_tenant_context(tenant_id, using=using):
        yield


async def aassert_rls_enforced(*, using: str = "default") -> None:
    """Async counterpart of :func:`assert_rls_enforced`.

    Runs the sync assertion in one ``thread_sensitive`` ``sync_to_async`` hop,
    so it checks the connection of the thread the hop lands on (see the testing
    guide, "Async Tests").

    Args:
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If the connection's current role is a superuser or has
            ``BYPASSRLS``.
    """
    await sync_to_async(assert_rls_enforced, thread_sensitive=True)(using=using)


async def aassert_rls_enabled(table_name: str, *, using: str = "default") -> None:
    """Async counterpart of :func:`assert_rls_enabled`.

    Runs the sync assertion in one ``thread_sensitive`` ``sync_to_async`` hop.

    Args:
        table_name: The database table name to check.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If the table does not exist, or RLS is not enabled or
            not forced on it.
    """
    await sync_to_async(assert_rls_enabled, thread_sensitive=True)(table_name, using=using)


async def aassert_rls_policy_exists(
    table_name: str,
    policy_name: str | None = None,
    *,
    using: str = "default",
) -> None:
    """Async counterpart of :func:`assert_rls_policy_exists`.

    Runs the sync assertion in one ``thread_sensitive`` ``sync_to_async`` hop.

    Args:
        table_name: The database table name to check.
        policy_name: Expected policy name. Defaults to
            ``"{table_name}_tenant_isolation_policy"``.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If the policy does not exist on the table.
    """
    await sync_to_async(assert_rls_policy_exists, thread_sensitive=True)(
        table_name,
        policy_name,
        using=using,
    )


async def aassert_rls_blocks_without_context(
    model_class: type[models.Model],
    *,
    using: str = "default",
) -> None:
    """Async counterpart of :func:`assert_rls_blocks_without_context`.

    Runs the sync assertion in one ``thread_sensitive`` ``sync_to_async``
    hop: the admin pre-check, the clearing of the admin and tenant GUCs, the
    no-context count and the restore all happen inside it, on the connection
    that holds the caller's GUCs. Like the sync helper it works with
    ``STRICT_MODE=True`` and inside :func:`async_rls_bypass` or
    :func:`async_rls_as_tenant`, and leaves their context intact. Requires at
    least one row in the table.

    Args:
        model_class: The RLS-protected model class to query.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If any rows are returned, or if the table is empty
            (which would make the assertion pass vacuously).
        RuntimeError: If ``USE_LOCAL_SET=True``, ``using`` is not in
            ``RLS_TENANTS["DATABASES"]`` and the hop thread's connection is
            not inside ``transaction.atomic()``.
    """
    await sync_to_async(assert_rls_blocks_without_context, thread_sensitive=True)(
        model_class,
        using=using,
    )
