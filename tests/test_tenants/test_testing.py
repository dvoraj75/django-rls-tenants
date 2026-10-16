"""Tests for django_rls_tenants.tenants.testing."""

from __future__ import annotations

import inspect
import threading
from contextlib import nullcontext
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, DataError, connection, connections, models, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.utils.connection import ConnectionDoesNotExist

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.context import bypass_flag
from django_rls_tenants.rls.guc import aget_guc, get_guc, set_guc
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
from tests.session_gucs import session_gucs
from tests.test_app.models import Order, ProtectedUser, Tenant

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Shared helpers and fixtures
# ---------------------------------------------------------------------------


def _read_gucs(using="default"):
    """Read both tenant GUCs on the database connection itself."""
    return get_guc("rls.is_admin", using=using), get_guc("rls.current_tenant", using=using)


async def _aread_gucs():
    """Read both tenant GUCs from a coroutine (one hop each)."""
    return await aget_guc("rls.is_admin"), await aget_guc("rls.current_tenant")


def _raw_order_count():
    """Count ``test_order`` rows with raw SQL: RLS applies, the ORM auto-scope filter does not."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM test_order")
        return cursor.fetchone()[0]


def _context_state():
    """Return the tenant ``ContextVar`` state: (tenant ID, context active flag)."""
    return get_current_tenant_id(), get_rls_context_active()


def _break_second_count(monkeypatch, fail):
    """Make the helper's second row count (the no-context one) call ``fail(using)`` instead."""
    count_rows = testing_mod._count_rows
    calls = []

    def count_or_fail(model_class, *, using):
        calls.append(using)
        if len(calls) == 2:
            return fail(using)
        return count_rows(model_class, using=using)

    monkeypatch.setattr(testing_mod, "_count_rows", count_or_fail)


@pytest.fixture
def strict_mode_on(settings):
    """Turn ``STRICT_MODE`` on; the config reloads on ``setting_changed``."""
    settings.RLS_TENANTS = {**settings.RLS_TENANTS, "STRICT_MODE": True}
    assert rls_tenants_config.STRICT_MODE is True


@pytest.fixture(params=[False, True], ids=["default_mode", "strict_mode"])
def strict_mode(request):
    """Run a test with ``STRICT_MODE`` off and on."""
    if request.param:
        request.getfixturevalue("strict_mode_on")
    assert rls_tenants_config.STRICT_MODE is request.param


@pytest.fixture
def myco_admin_policy(request, rls_role, db):
    """Add a ``test_order`` policy that admits every row while ``myco.is_admin`` is ``'true'``.

    The policy is created inside the test transaction (the rollback drops it)
    as the connecting role, a superuser or the table owner. ``enforce_rls``
    is entered only afterwards, because the RLS test role may not create
    policies; ``rls_role`` comes first so that the role is created outside
    the test transaction, where the rollback cannot drop it.
    """
    with connection.cursor() as cur:
        cur.execute(
            "CREATE POLICY test_order_myco_admin ON test_order "
            "USING (current_setting('myco.is_admin', true) = 'true')",
        )
    request.getfixturevalue("enforce_rls")


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

    def test_passes_under_strict_mode(self, enforce_rls, sample_orders, strict_mode_on):
        """Works with STRICT_MODE=True, where an ORM query with no context raises (#126)."""
        with pytest.raises(NoTenantContextError, match="strict mode"):
            Order.objects.count()
        assert_rls_blocks_without_context(Order)
        assert _read_gucs() == (None, None)
        assert _context_state() == (None, False)

    def test_fails_on_empty_table(self, strict_mode):
        """Fails instead of passing vacuously when the table is empty."""
        with pytest.raises(AssertionError, match="the table is empty"):
            assert_rls_blocks_without_context(Order)

    def test_fails_without_rls(self, tenant_a, strict_mode):
        """Fails on a table without RLS, where rows stay visible with no context."""
        with pytest.raises(AssertionError, match=r"table 'test_tenant'.*RLS may not be enforced"):
            assert_rls_blocks_without_context(Tenant)

    def test_passes_inside_rls_bypass(self, enforce_rls, sample_orders):
        """The admin bypass is lifted for the count only and is intact afterwards (#126)."""
        with rls_bypass():
            assert_rls_blocks_without_context(Order)
            assert _read_gucs() == ("true", None)
            assert _raw_order_count() == 3
            assert _context_state() == (None, True)

    def test_passes_inside_rls_as_tenant(self, enforce_rls, sample_orders, tenant_a):
        """The tenant scope is lifted for the count only and is intact afterwards."""
        with rls_as_tenant(tenant_a.pk):
            assert_rls_blocks_without_context(Order)
            assert _read_gucs() == ("false", str(tenant_a.pk))
            assert _raw_order_count() == 2
            assert _context_state() == (tenant_a.pk, True)

    def test_clears_the_gucs_of_the_configured_prefix(
        self,
        myco_admin_policy,
        sample_orders,
        settings,
    ):
        """The cleared GUCs follow ``GUC_PREFIX``; a hard-coded ``rls.`` would leave ``myco.``."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "GUC_PREFIX": "myco"}
        with rls_bypass():
            assert _raw_order_count() == 3  # admitted by the myco policy alone
            assert_rls_blocks_without_context(Order)
            assert get_guc("myco.is_admin") == "true"

    def test_leaves_bypass_flags_set(self, enforce_rls, sample_protected_users):
        """Only the admin and tenant GUCs are cleared: a bypass flag still makes rows visible."""
        with bypass_flag("rls.is_login_request"):
            with pytest.raises(AssertionError, match="got 2"):
                assert_rls_blocks_without_context(ProtectedUser)
            assert get_guc("rls.is_login_request") == "true"
        assert_rls_blocks_without_context(ProtectedUser)

    def test_quotes_the_table_name(self):
        """A table named after a reserved word (``order``) is counted, not a syntax error."""
        with connection.cursor() as cur:
            cur.execute('CREATE TABLE "order" (id serial PRIMARY KEY)')
            cur.execute('INSERT INTO "order" DEFAULT VALUES')
        with isolate_apps("tests.test_app"):

            class Keyword(models.Model):  # noqa: DJ008  -- throwaway model, never printed
                class Meta:
                    app_label = "test_app"
                    db_table = "order"
                    managed = False

            with pytest.raises(AssertionError, match=r"table 'order'.*got 1"):
                assert_rls_blocks_without_context(Keyword)

    def test_restores_context_when_the_assertion_fails(self, tenant_a):
        """A failing assertion leaves the caller's context as it was."""
        with rls_as_tenant(tenant_a.pk):
            with pytest.raises(AssertionError, match="RLS may not be enforced"):
                assert_rls_blocks_without_context(Tenant)
            assert _read_gucs() == ("false", str(tenant_a.pk))
            assert _context_state() == (tenant_a.pk, True)

    def test_restores_context_when_the_count_raises(self, monkeypatch, sample_orders):
        """An error in the no-context count still restores the caller's GUCs."""

        def boom(using):
            raise RuntimeError("boom")

        _break_second_count(monkeypatch, boom)
        with rls_bypass():
            with pytest.raises(RuntimeError, match="boom"):
                assert_rls_blocks_without_context(Order)
            assert _read_gucs() == ("true", None)
            assert _context_state() == (None, True)

    @pytest.mark.parametrize("use_local_set", [False, True], ids=["set_config", "set_local"])
    def test_database_error_in_the_count_keeps_the_context(
        self,
        monkeypatch,
        sample_orders,
        settings,
        use_local_set,
    ):
        """A failed count surfaces as itself, and the caller's context survives the rollback.

        The error aborts the transaction, so the restore cannot run; rolling
        back to the savepoint undoes the cleared GUCs instead.
        """
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": use_local_set}

        def divide_by_zero(using):
            with connections[using].cursor() as cursor:
                cursor.execute("SELECT 1/0")

        _break_second_count(monkeypatch, divide_by_zero)
        with rls_bypass():
            with pytest.raises(DataError, match="division by zero"), transaction.atomic():
                assert_rls_blocks_without_context(Order)
            assert _read_gucs() == ("true", None)
            assert _context_state() == (None, True)

    @pytest.mark.django_db(transaction=True)
    def test_use_local_set_leaves_no_bypass_after_the_commit(
        self, enforce_rls, sample_orders, settings
    ):
        """With USE_LOCAL_SET=True the admin bypass does not outlive the commit.

        The helper clears and restores the GUCs for the session around the
        count (#137: the same path inside ``atomic()`` as outside it), so the
        caller's context must come from the GUC affinity guard, which takes it
        off the connection when ``rls_bypass()`` exits.
        """
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with transaction.atomic(), rls_bypass():
            assert_rls_blocks_without_context(Order)
            assert _read_gucs() == ("true", None)
            assert _raw_order_count() == 3
        assert _read_gucs() == (None, None)

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.parametrize("caller", ["none", "bypass", "tenant"])
    def test_use_local_set_outside_a_transaction(
        self,
        enforce_rls,
        sample_orders,
        settings,
        tenant_a,
        caller,
    ):
        """No transaction: the caller's context is in the ContextVars only, and survives (#96)."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        expected = {
            "none": ((None, None), (None, False), 0),
            "bypass": (("true", None), (None, True), 3),
            "tenant": (("false", str(tenant_a.pk)), (tenant_a.pk, True), 2),
        }[caller]
        block = {"none": nullcontext, "bypass": rls_bypass, "tenant": None}[caller]
        with rls_as_tenant(tenant_a.pk) if block is None else block():
            assert_rls_blocks_without_context(Order)
            seen = _read_gucs(), _context_state(), _raw_order_count()
        assert seen == expected
        assert session_gucs() == (None, None)
        assert _context_state() == (None, False)

    @pytest.mark.django_db(transaction=True)
    def test_use_local_set_session_values_are_cleared_for_the_count(
        self,
        enforce_rls,
        sample_orders,
        settings,
    ):
        """Values set for the session by hand do not reach the count.

        The helper's admin pre-check is a context: once it ends, the session's
        own values count as unknown, and the guard clears them before the
        next statement run outside a context (#96).
        """
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        set_guc("rls.is_admin", "true")
        assert _raw_order_count() == 3
        assert_rls_blocks_without_context(Order)
        assert session_gucs() == (None, None)
        assert _raw_order_count() == 0

    @pytest.mark.django_db(transaction=True)
    def test_use_local_set_alias_outside_rls_databases_raises(
        self,
        replica,
        sample_orders,
        settings,
    ):
        """The guard leaves that alias alone, so ``SET LOCAL`` still needs a transaction."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with pytest.raises(RuntimeError, match="SET LOCAL"):
            assert_rls_blocks_without_context(Order, using=replica)
        assert _context_state() == (None, False)

    @pytest.mark.django_db(transaction=True)
    def test_runs_every_query_on_the_using_alias(self, replica, sample_orders):
        """The pre-check, the GUC clear, the count and the restore all use ``using``."""
        with (
            CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as on_default,
            CaptureQueriesContext(connections[replica]) as on_replica,
            rls_bypass(using=replica),
        ):
            assert_rls_blocks_without_context(Order, using=replica)
            assert _read_gucs(using=replica) == ("true", None)
        assert on_default.captured_queries == []
        counts = [q["sql"] for q in on_replica.captured_queries if "test_order" in q["sql"]]
        assert len(counts) == 2


# ---------------------------------------------------------------------------
# Async helpers (#40), driven through async_to_sync so the hops run on the
# test thread and use the test's connection and transaction.
# ---------------------------------------------------------------------------


class TestAsyncRlsBypass:
    """Tests for async_rls_bypass()."""

    def test_enables_admin_context(self):
        """Sets the admin GUCs and the admin ContextVar state inside the block."""

        async def run():
            async with async_rls_bypass():
                return await _aread_gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == (("true", None), None, True)

    def test_clears_on_exit(self):
        """GUCs and ContextVar state are empty again after the block."""

        async def run():
            async with async_rls_bypass():
                pass
            return await _aread_gucs(), get_current_tenant_id(), get_rls_context_active()

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
                return await _aread_gucs(), get_current_tenant_id()

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
                return await _aread_gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == (("false", str(tenant_a.pk)), tenant_a.pk, True)

    def test_clears_on_exit(self, tenant_a):
        """GUCs and ContextVar state are empty again after the block."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                pass
            return await _aread_gucs(), get_current_tenant_id(), get_rls_context_active()

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

    def test_blocks_without_context_passes_under_strict_mode(
        self,
        enforce_rls,
        sample_orders,
        strict_mode_on,
    ):
        """Works with STRICT_MODE=True, like the sync helper (#126)."""

        async def unscoped_count():
            return await Order.objects.acount()

        with pytest.raises(NoTenantContextError, match="strict mode"):
            async_to_sync(unscoped_count)()
        async_to_sync(aassert_rls_blocks_without_context)(Order)

    def test_blocks_without_context_inside_async_rls_bypass(self, enforce_rls, sample_orders):
        """Passes inside async_rls_bypass() and leaves the bypass intact."""

        async def run():
            async with async_rls_bypass():
                await aassert_rls_blocks_without_context(Order)
                return (
                    await _aread_gucs(),
                    _context_state(),
                    await sync_to_async(_raw_order_count, thread_sensitive=True)(),
                )

        assert async_to_sync(run)() == (("true", None), (None, True), 3)

    def test_blocks_without_context_inside_async_rls_as_tenant(
        self,
        enforce_rls,
        sample_orders,
        tenant_a,
    ):
        """Passes inside async_rls_as_tenant() and leaves the tenant scope intact."""

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                await aassert_rls_blocks_without_context(Order)
                return (
                    await _aread_gucs(),
                    _context_state(),
                    await sync_to_async(_raw_order_count, thread_sensitive=True)(),
                )

        expected = (("false", str(tenant_a.pk)), (tenant_a.pk, True), 2)
        assert async_to_sync(run)() == expected

    @pytest.mark.django_db(transaction=True)
    def test_blocks_without_context_use_local_set_outside_a_transaction(
        self,
        enforce_rls,
        sample_orders,
        settings,
        tenant_a,
    ):
        """With USE_LOCAL_SET=True and no transaction on the hop thread it works too (#96)."""
        settings.RLS_TENANTS = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}

        async def run():
            async with async_rls_as_tenant(tenant_a.pk):
                await aassert_rls_blocks_without_context(Order)
                return (
                    await _aread_gucs(),
                    await sync_to_async(_raw_order_count, thread_sensitive=True)(),
                )

        assert async_to_sync(run)() == (("false", str(tenant_a.pk)), 2)
        assert session_gucs() == (None, None)


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
                assert await _aread_gucs() == ("false", str(self.tenant_a.pk))
                assert await sync_to_async(_raw_order_count, thread_sensitive=True)() == 1
            assert await _aread_gucs() == (None, None)
