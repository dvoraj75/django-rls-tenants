"""Tests for django_rls_tenants.rls.guc."""

from __future__ import annotations

import asyncio
import threading

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connections, transaction

from django_rls_tenants.rls.guc import (
    aclear_guc,
    aget_guc,
    arestore_guc,
    aset_guc,
    clear_guc,
    get_guc,
    set_guc,
)

pytestmark = pytest.mark.django_db


def test_set_get_roundtrip():
    """set_guc then get_guc returns the value."""
    set_guc("rls.test_var", "hello")
    assert get_guc("rls.test_var") == "hello"


def test_get_unset_returns_none():
    """get_guc for an unset variable returns None."""
    assert get_guc("rls.never_set_variable") is None


def test_clear_guc():
    """After clear_guc, get_guc returns None."""
    set_guc("rls.test_clear", "value")
    clear_guc("rls.test_clear")
    assert get_guc("rls.test_clear") is None


@pytest.mark.django_db(transaction=True)
def test_is_local_outside_transaction_raises():
    """set_guc with is_local=True outside atomic() raises RuntimeError."""
    with pytest.raises(RuntimeError, match="Cannot use SET LOCAL"):
        set_guc("rls.test_local", "value", is_local=True)


@pytest.mark.django_db(transaction=True)
def test_is_local_inside_transaction_works():
    """set_guc with is_local=True inside atomic() works and auto-clears."""
    with transaction.atomic():
        set_guc("rls.test_local_ok", "inside", is_local=True)
        assert get_guc("rls.test_local_ok") == "inside"
    # After transaction ends, SET LOCAL value is gone
    assert get_guc("rls.test_local_ok") is None


def test_session_guc_survives_multiple_queries():
    """Session-level GUC persists across multiple cursor operations."""
    set_guc("rls.test_persist", "persistent")
    # Multiple get_guc calls (each opens a new cursor)
    assert get_guc("rls.test_persist") == "persistent"
    assert get_guc("rls.test_persist") == "persistent"


# ---------------------------------------------------------------------------
# GUC name validation (SQL injection prevention)
# ---------------------------------------------------------------------------


class TestGucNameValidation:
    """Tests for _validate_guc_name rejecting invalid GUC names."""

    def test_sql_injection_semicolon(self):
        """GUC name with SQL injection via semicolon is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("; DROP TABLE users", "val")

    def test_sql_injection_comment(self):
        """GUC name with SQL comment injection is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls.tenant; --", "val")

    def test_empty_string(self):
        """Empty string is rejected as GUC name."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("", "val")

    def test_spaces(self):
        """GUC name with spaces is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls current_tenant", "val")

    def test_single_quotes(self):
        """GUC name with single quotes is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls'tenant", "val")

    def test_parentheses(self):
        """GUC name with parentheses is rejected."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls()", "val")

    def test_trailing_newline(self):
        """A trailing newline is rejected (\\Z anchor, not $, which allows it)."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            set_guc("rls.current_tenant\n", "val")

    def test_get_guc_also_validates(self):
        """get_guc rejects invalid names too."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            get_guc("; DROP TABLE users")

    def test_clear_guc_also_validates(self):
        """clear_guc rejects invalid names too."""
        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            clear_guc("; DROP TABLE users")

    def test_valid_dotted_name_accepted(self):
        """Valid dotted GUC names like 'myapp.is_admin' are accepted."""
        set_guc("myapp.is_admin", "true")
        assert get_guc("myapp.is_admin") == "true"
        clear_guc("myapp.is_admin")

    def test_valid_underscore_start_accepted(self):
        """GUC names starting with underscore are accepted."""
        set_guc("_internal.flag", "yes")
        assert get_guc("_internal.flag") == "yes"
        clear_guc("_internal.flag")


# ---------------------------------------------------------------------------
# Async primitives (#36)
# ---------------------------------------------------------------------------


class TestAsyncGucPrimitives:
    """aset_guc / aget_guc / aclear_guc / arestore_guc driven through async_to_sync.

    Under ``async_to_sync`` the thread-sensitive hop runs on the test thread,
    so the coroutine shares the test's connection and transaction.
    """

    def test_aset_aget_roundtrip(self):
        """aset_guc then aget_guc returns the value."""

        async def run():
            await aset_guc("rls.test_async", "hello")
            return await aget_guc("rls.test_async")

        assert async_to_sync(run)() == "hello"

    def test_aget_unset_returns_none(self):
        """aget_guc for an unset variable returns None."""
        assert async_to_sync(aget_guc)("rls.never_set_async") is None

    def test_aclear_guc(self):
        """After aclear_guc, aget_guc returns None."""

        async def run():
            await aset_guc("rls.test_async_clear", "value")
            await aclear_guc("rls.test_async_clear")
            return await aget_guc("rls.test_async_clear")

        assert async_to_sync(run)() is None

    @pytest.mark.parametrize("previous", ["before", None], ids=["value", "none"])
    def test_arestore_guc(self, previous):
        """arestore_guc reinstates a saved value, or clears for None."""

        async def run():
            await aset_guc("rls.test_async_restore", "changed")
            await arestore_guc("rls.test_async_restore", previous)
            return await aget_guc("rls.test_async_restore")

        assert async_to_sync(run)() == previous

    def test_using_keyword_accepted(self):
        """The ``using`` alias keyword is passed through to the sync function."""

        async def run():
            await aset_guc("rls.test_async_using", "v", using="default")
            value = await aget_guc("rls.test_async_using", using="default")
            await aclear_guc("rls.test_async_using", using="default")
            await arestore_guc("rls.test_async_using", "w", using="default")
            return value, await aget_guc("rls.test_async_using", using="default")

        assert async_to_sync(run)() == ("v", "w")

    @pytest.mark.parametrize(
        "call",
        [
            lambda name: aset_guc(name, "v"),
            aget_guc,
            aclear_guc,
            lambda name: arestore_guc(name, "v"),
        ],
        ids=["aset_guc", "aget_guc", "aclear_guc", "arestore_guc"],
    )
    def test_name_validation_propagates(self, call):
        """The sync function's ValueError surfaces unchanged through the await."""

        async def run():
            await call("; DROP TABLE users")

        with pytest.raises(ValueError, match="Invalid GUC variable name"):
            async_to_sync(run)()

    def test_sync_and_async_share_the_connection(self):
        """Values set by sync code are read by aget_guc and vice versa."""
        set_guc("rls.test_mixed", "from_sync")
        assert async_to_sync(aget_guc)("rls.test_mixed") == "from_sync"
        async_to_sync(aset_guc)("rls.test_mixed", "from_async")
        assert get_guc("rls.test_mixed") == "from_async"

    @pytest.mark.django_db(transaction=True)
    def test_is_local_inside_atomic_works(self):
        """is_local=True works when the calling sync code holds atomic() open."""

        async def run():
            await aset_guc("rls.test_async_local", "local_val", is_local=True)
            return await aget_guc("rls.test_async_local")

        with transaction.atomic():
            assert async_to_sync(run)() == "local_val"
            assert get_guc("rls.test_async_local") == "local_val"
        assert get_guc("rls.test_async_local") is None


@pytest.mark.django_db(transaction=True)
def test_async_primitives_run_on_their_own_connection_under_a_bare_loop():
    """Without async_to_sync the hop runs on an executor thread with its own connection."""
    test_thread = threading.get_ident()

    async def run():
        hop_thread = await sync_to_async(threading.get_ident, thread_sensitive=True)()
        try:
            await aset_guc("rls.test_bare_loop", "v")
            value = await aget_guc("rls.test_bare_loop")
            with pytest.raises(RuntimeError, match="Cannot use SET LOCAL"):
                await aset_guc("rls.test_bare_loop", "w", is_local=True)
            return hop_thread, value
        finally:
            await sync_to_async(connections.close_all, thread_sensitive=True)()

    hop_thread, value = asyncio.run(run())
    assert hop_thread != test_thread
    assert value == "v"
    # The GUC lived on the hop thread's connection, not on the test's.
    assert get_guc("rls.test_bare_loop") is None
