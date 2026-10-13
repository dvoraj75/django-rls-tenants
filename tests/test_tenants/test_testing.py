"""Tests for django_rls_tenants.tenants.testing."""

from __future__ import annotations

import inspect
import threading
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.utils.connection import ConnectionDoesNotExist

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import aget_guc, get_guc
from django_rls_tenants.tenants import testing as testing_mod
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import admin_context
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from django_rls_tenants.tenants.testing import (
    aassert_rls_blocks_without_context,
    aassert_rls_enabled,
    aassert_rls_policy_exists,
    assert_rls_blocks_without_context,
    assert_rls_enabled,
    assert_rls_policy_exists,
    async_rls_as_tenant,
    async_rls_bypass,
    rls_as_tenant,
    rls_bypass,
)
from tests.test_app.models import Order, Tenant

pytestmark = pytest.mark.django_db


class TestRlsBypass:
    """Tests for rls_bypass() context manager."""

    def test_enables_admin_context(self):
        """rls_bypass sets admin GUC variables."""
        with rls_bypass():
            assert get_guc("rls.is_admin") == "true"
            # Admin mode clears tenant GUC; admin_bypass clause handles access.
            assert get_guc("rls.current_tenant") is None

    def test_clears_on_exit(self):
        """GUCs are cleared after rls_bypass exits."""
        with rls_bypass():
            pass
        assert get_guc("rls.is_admin") is None


class TestRlsAsTenant:
    """Tests for rls_as_tenant() context manager."""

    def test_scopes_to_tenant(self, tenant_a):
        """rls_as_tenant sets the correct tenant GUC."""
        with rls_as_tenant(tenant_a.pk):
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)
            assert get_guc("rls.is_admin") == "false"

    def test_clears_on_exit(self, tenant_a):
        """GUCs are cleared after rls_as_tenant exits."""
        with rls_as_tenant(tenant_a.pk):
            pass
        assert get_guc("rls.current_tenant") is None


class TestAssertRlsEnabled:
    """Tests for assert_rls_enabled()."""

    def test_passes_for_rls_table(self):
        """Passes for a table with RLS enabled and forced."""
        assert_rls_enabled("test_order")

    def test_fails_for_non_rls_table(self):
        """Fails for a table without RLS."""
        with pytest.raises(AssertionError, match="RLS is not enabled"):
            assert_rls_enabled("test_tenant")

    def test_fails_for_nonexistent_table(self):
        """Fails for a table that doesn't exist."""
        with pytest.raises(AssertionError, match="does not exist"):
            assert_rls_enabled("nonexistent_table_xyz")


class TestAssertRlsPolicyExists:
    """Tests for assert_rls_policy_exists()."""

    def test_passes_with_default_policy_name(self):
        """Passes when the default isolation policy exists."""
        assert_rls_policy_exists("test_order")

    def test_passes_with_explicit_policy_name(self):
        """Passes with an explicit policy name."""
        assert_rls_policy_exists("test_order", "test_order_tenant_isolation_policy")

    def test_fails_for_wrong_policy_name(self):
        """Fails when the specified policy doesn't exist."""
        with pytest.raises(AssertionError, match="does not exist"):
            assert_rls_policy_exists("test_order", "nonexistent_policy")


class TestAssertRlsBlocksWithoutContext:
    """Tests for assert_rls_blocks_without_context()."""

    def test_passes_when_no_rows_visible(self, enforce_rls, sample_orders):
        """Passes when RLS blocks all rows (no GUC context set)."""
        # sample_orders fixture creates data but no context is active here
        assert_rls_blocks_without_context(Order)


# ---------------------------------------------------------------------------
# Async helpers (#40), driven through async_to_sync so the hops run on the
# test thread and use the test's connection and transaction.
# ---------------------------------------------------------------------------


async def _gucs():
    """Read both tenant GUCs from a coroutine (one hop each)."""
    return await aget_guc("rls.is_admin"), await aget_guc("rls.current_tenant")


def _raw_order_count():
    """Count ``test_order`` rows with raw SQL: RLS applies, the ORM auto-scope filter does not."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM test_order")
        return cursor.fetchone()[0]


class TestAsyncRlsBypass:
    """Tests for async_rls_bypass()."""

    def test_enables_admin_context(self):
        """Sets the admin GUCs and the admin ContextVar state inside the block."""

        async def run():
            async with async_rls_bypass():
                return await _gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == (("true", None), None, True)

    def test_clears_on_exit(self):
        """GUCs and ContextVar state are empty again after the block."""

        async def run():
            async with async_rls_bypass():
                pass
            return await _gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == ((None, None), None, False)

    def test_restores_on_exception(self):
        """An exception in the block propagates and still restores the GUCs and the state."""

        async def run():
            async with async_rls_bypass():
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            async_to_sync(run)()
        assert get_guc("rls.is_admin") is None
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    def test_restores_enclosing_tenant_scope(self, tenant_a):
        """An inner bypass block does not leak admin access into the enclosing tenant block."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                async with async_rls_bypass():
                    pass
                return await _gucs(), get_current_tenant_id()

        assert async_to_sync(run)() == (("false", str(tenant_a.pk)), tenant_a.pk)

    def test_sees_every_tenant(self, enforce_rls, sample_orders):
        """The async ORM and raw SQL both see all three rows with RLS enforced."""

        async def run():
            async with async_rls_bypass():
                return (
                    await Order.objects.acount(),
                    await sync_to_async(_raw_order_count, thread_sensitive=True)(),
                )

        assert async_to_sync(run)() == (3, 3)


class TestAsyncRlsAsTenant:
    """Tests for async_rls_as_tenant()."""

    def test_scopes_to_tenant(self, tenant_a):
        """Sets the tenant GUCs and the tenant ContextVar state inside the block."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                return await _gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == (("false", str(tenant_a.pk)), tenant_a.pk, True)

    def test_clears_on_exit(self, tenant_a):
        """GUCs and ContextVar state are empty again after the block."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                pass
            return await _gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == ((None, None), None, False)

    def test_restores_on_exception(self, tenant_a):
        """An exception in the block still restores the GUCs and the ContextVar state."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            async_to_sync(run)()
        assert get_guc("rls.current_tenant") is None
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    def test_none_raises(self):
        """tenant_id=None raises NoTenantContextError before the block runs."""

        async def run():
            async with async_rls_as_tenant(None):
                pytest.fail("The block must not run")

        with pytest.raises(NoTenantContextError, match="tenant_id cannot be None"):
            async_to_sync(run)()

    def test_database_filters_other_tenants(self, enforce_rls, sample_orders, tenant_a):
        """Raw SQL sees only the tenant's rows, so the GUCs are on the test's connection."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                return (
                    await Order.objects.acount(),
                    await sync_to_async(_raw_order_count, thread_sensitive=True)(),
                )

        assert async_to_sync(run)() == (2, 2)


async def _enter(manager):
    async with manager:
        pytest.fail("The block must not run")


@pytest.mark.parametrize(
    "call",
    [
        lambda: _enter(async_rls_bypass(using="nope")),
        lambda: _enter(async_rls_as_tenant(1, using="nope")),
        lambda: aassert_rls_enabled("test_order", using="nope"),
        lambda: aassert_rls_policy_exists("test_order", using="nope"),
        lambda: aassert_rls_blocks_without_context(Order, using="nope"),
    ],
    ids=["bypass", "as_tenant", "enabled", "policy_exists", "blocks_without_context"],
)
def test_async_helpers_use_the_given_alias(call):
    """Every async helper runs its SQL on the ``using`` alias, not on ``default``."""

    async def run():
        await call()

    with pytest.raises(ConnectionDoesNotExist, match="nope"):
        async_to_sync(run)()
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


@pytest.mark.parametrize(
    ("helper", "target", "args"),
    [
        (aassert_rls_enabled, "assert_rls_enabled", ("tbl",)),
        (aassert_rls_policy_exists, "assert_rls_policy_exists", ("tbl", "pol")),
        (aassert_rls_blocks_without_context, "assert_rls_blocks_without_context", (Order,)),
    ],
    ids=["enabled", "policy_exists", "blocks_without_context"],
)
def test_async_assertions_call_the_sync_one_on_the_test_thread(monkeypatch, helper, target, args):
    """Each assertion calls its sync version once, with every argument, on the test thread.

    A hop on another thread would use another connection, outside the test's
    transaction. For the two catalog-only assertions nothing else would notice.
    """
    signature = inspect.signature(getattr(testing_mod, target))
    calls = []

    def spy(*spy_args, **spy_kwargs):
        calls.append((signature.bind(*spy_args, **spy_kwargs).arguments, threading.get_ident()))

    monkeypatch.setattr(testing_mod, target, spy)
    async_to_sync(helper)(*args, using="replica")
    assert calls == [(signature.bind(*args, using="replica").arguments, threading.get_ident())]


class TestAsyncAssertions:
    """Tests for the aassert_* helpers."""

    def test_rls_enabled_passes_for_rls_table(self):
        """Passes for a table with RLS enabled and forced."""
        async_to_sync(aassert_rls_enabled)("test_order")

    def test_rls_enabled_fails_for_non_rls_table(self):
        """Fails for a table without RLS."""
        with pytest.raises(AssertionError, match="RLS is not enabled"):
            async_to_sync(aassert_rls_enabled)("test_tenant")

    def test_rls_enabled_fails_for_nonexistent_table(self):
        """Fails for a table that does not exist."""
        with pytest.raises(AssertionError, match="does not exist"):
            async_to_sync(aassert_rls_enabled)("nonexistent_table_xyz")

    def test_policy_exists_with_default_name(self):
        """Passes when the default isolation policy exists."""
        async_to_sync(aassert_rls_policy_exists)("test_order")

    def test_policy_exists_with_explicit_name(self):
        """Passes with an explicit policy name given positionally."""
        async_to_sync(aassert_rls_policy_exists)(
            "test_order",
            "test_order_tenant_isolation_policy",
        )

    def test_policy_exists_fails_for_wrong_name(self):
        """Fails when the named policy does not exist."""
        with pytest.raises(AssertionError, match="does not exist"):
            async_to_sync(aassert_rls_policy_exists)("test_order", "nonexistent_policy")

    def test_blocks_without_context_passes(self, enforce_rls, sample_orders):
        """Passes when RLS hides every row and no context is active."""
        async_to_sync(aassert_rls_blocks_without_context)(Order)

    def test_blocks_without_context_fails_on_empty_table(self):
        """Fails instead of passing vacuously when the table is empty."""
        with pytest.raises(AssertionError, match="the table is empty"):
            async_to_sync(aassert_rls_blocks_without_context)(Order)

    def test_blocks_without_context_fails_without_rls(self, tenant_a):
        """Fails on a table without RLS, where rows stay visible with no context."""
        with pytest.raises(AssertionError, match="RLS may not be enforced"):
            async_to_sync(aassert_rls_blocks_without_context)(Tenant)


@pytest.mark.usefixtures("enforce_rls")
class TestAsyncHelpersInDjangoTestCase(TestCase):
    """The path the testing guide recommends: ``async def`` methods on a Django ``TestCase``.

    Django runs those methods through ``async_to_sync``, so the helpers' hops
    land on the test thread and see the rows ``setUpTestData`` created inside
    the class transaction. ``enforce_rls`` makes the raw counts depend on the
    RLS policy on a superuser connection too, not only on the ORM filter.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Tenant A")
        cls.tenant_b = Tenant.objects.create(name="Tenant B")
        with admin_context():
            Order.objects.create(product="A1", amount=Decimal("1.00"), tenant=cls.tenant_a)
            Order.objects.create(product="B1", amount=Decimal("2.00"), tenant=cls.tenant_b)

    async def test_helpers_see_the_test_transaction(self):
        """Rows from setUpTestData are visible and scoped inside the async helpers."""
        raw_count = sync_to_async(_raw_order_count, thread_sensitive=True)
        async with async_rls_as_tenant(self.tenant_a.pk):
            assert await Order.objects.acount() == 1
            assert await raw_count() == 1
        async with async_rls_bypass():
            assert await Order.objects.acount() == 2
            assert await raw_count() == 2
        await aassert_rls_enabled("test_order")
        await aassert_rls_policy_exists("test_order")
        await aassert_rls_blocks_without_context(Order)

    async def test_use_local_set_works_inside_the_test_transaction(self):
        """USE_LOCAL_SET=True works: the test transaction is open on the hop thread."""
        with self.settings(RLS_TENANTS={**settings.RLS_TENANTS, "USE_LOCAL_SET": True}):
            assert rls_tenants_config.USE_LOCAL_SET is True
            async with async_rls_as_tenant(self.tenant_a.pk):
                assert await _gucs() == ("false", str(self.tenant_a.pk))
                assert await sync_to_async(_raw_order_count, thread_sensitive=True)() == 1
            assert await _gucs() == (None, None)
