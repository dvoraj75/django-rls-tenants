"""Test helpers for verifying RLS behavior.

Provides context managers for test setup (``rls_bypass``, ``rls_as_tenant``)
and assertion functions for verifying RLS policies are applied correctly.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connections

from django_rls_tenants.tenants.context import (
    admin_context,
    tenant_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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
