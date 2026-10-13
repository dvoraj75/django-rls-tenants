"""Test helpers for verifying RLS behavior.

Provides context managers for test setup (``rls_bypass``, ``rls_as_tenant``)
and assertion functions for verifying RLS policies are applied correctly,
plus their counterparts for ``async def`` tests (``async_rls_bypass``,
``async_rls_as_tenant``, ``aassert_rls_enabled``, ``aassert_rls_policy_exists``
and ``aassert_rls_blocks_without_context``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import connections

from django_rls_tenants.tenants.context import (
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
)

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


def assert_rls_blocks_without_context(
    model_class: type[models.Model],
    *,
    using: str = "default",
) -> None:
    """Assert that querying with no GUC context returns zero rows.

    Verifies the fail-closed behavior. Requires at least one row
    to exist in the table (caller must set up test data first).

    Args:
        model_class: The RLS-protected model class to query.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If any rows are returned, or if the table is empty
            (which would make the assertion pass vacuously).
    """
    # Pre-check: verify the table has data (via admin bypass) so the
    # assertion is not vacuously true on an empty table.
    with admin_context(using=using):
        total = model_class.objects.using(using).count()  # type: ignore[attr-defined]
    assert total > 0, (
        f"assert_rls_blocks_without_context requires at least one row in "
        f"{model_class.__name__}, but the table is empty. "
        f"Set up test data before calling this helper."
    )

    qs = model_class.objects.using(using).all()  # type: ignore[attr-defined]
    count = qs.count()
    assert count == 0, (
        f"Expected 0 rows from {model_class.__name__} without "
        f"RLS context, got {count}. RLS may not be enforced."
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

    Usage::

        async with async_rls_as_tenant(tenant_id=42):
            orders = [order async for order in Order.objects.all()]  # only tenant 42
    """
    async with async_tenant_context(tenant_id, using=using):
        yield


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
    hop: the admin pre-check and the unscoped count both happen inside it,
    and the count sees the caller's RLS state exactly as the sync helper
    does. Requires at least one row in the table.

    Args:
        model_class: The RLS-protected model class to query.
        using: Database alias. Default: ``"default"``.

    Raises:
        AssertionError: If any rows are returned, or if the table is empty
            (which would make the assertion pass vacuously).
    """
    await sync_to_async(assert_rls_blocks_without_context, thread_sensitive=True)(
        model_class,
        using=using,
    )
