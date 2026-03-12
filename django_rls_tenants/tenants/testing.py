"""Test helpers for verifying RLS behavior.

Provides context managers for test setup (``rls_bypass``, ``rls_as_tenant``)
and assertion functions for verifying RLS policies are applied correctly.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connection

from django_rls_tenants.tenants.context import (
    admin_context,
    tenant_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db import models


@contextmanager
def rls_bypass() -> Iterator[None]:
    """Temporarily enable admin bypass for tests.

    Usage::

        with rls_bypass():
            all_orders = Order.objects.all()  # sees all tenants
    """
    with admin_context():
        yield


@contextmanager
def rls_as_tenant(tenant_id: int | str) -> Iterator[None]:
    """Scope to a specific tenant for tests.

    Usage::

        with rls_as_tenant(tenant_id=42):
            orders = Order.objects.all()  # only tenant 42
    """
    with tenant_context(tenant_id):
        yield


def assert_rls_enabled(table_name: str) -> None:
    """Assert that RLS is enabled and forced on the given table.

    Args:
        table_name: The database table name to check.

    Raises:
        AssertionError: If RLS is not enabled or not forced.
    """
    with connection.cursor() as cursor:
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
) -> None:
    """Assert that an RLS policy exists on the given table.

    Args:
        table_name: The database table name to check.
        policy_name: Expected policy name. Defaults to
            ``"{table_name}_tenant_isolation_policy"``.
    """
    if policy_name is None:
        policy_name = f"{table_name}_tenant_isolation_policy"

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_policies WHERE tablename = %s AND policyname = %s",
            [table_name, policy_name],
        )
        assert cursor.fetchone() is not None, (
            f"RLS policy '{policy_name}' does not exist on table '{table_name}'"
        )


def assert_rls_blocks_without_context(
    model_class: type[models.Model],
) -> None:
    """Assert that querying with no GUC context returns zero rows.

    Verifies the fail-closed behavior. Requires at least one row
    to exist in the table (caller must set up test data first).

    Args:
        model_class: The RLS-protected model class to query.

    Raises:
        AssertionError: If any rows are returned.
    """
    qs = model_class.objects.all()  # type: ignore[attr-defined]
    count = qs.count()
    assert count == 0, (
        f"Expected 0 rows from {model_class.__name__} without "
        f"RLS context, got {count}. RLS may not be enforced."
    )
