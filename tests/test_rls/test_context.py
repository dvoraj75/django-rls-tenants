"""Tests for django_rls_tenants.rls.context."""

from __future__ import annotations

import asyncio
import time

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import DataError, transaction

from django_rls_tenants.rls.context import (
    _enter_gucs,
    async_bypass_flag,
    async_rls_context,
    bypass_flag,
    restore_gucs,
    rls_context,
)
from django_rls_tenants.rls.guc import aget_guc, get_guc, set_guc

pytestmark = pytest.mark.django_db


def test_rls_context_sets_and_restores():
    """rls_context sets GUCs in block and restores previous values on exit."""
    set_guc("rls.test_ctx", "original")
    with rls_context({"rls.test_ctx": "inside"}):
        assert get_guc("rls.test_ctx") == "inside"
    assert get_guc("rls.test_ctx") == "original"


def test_rls_context_clears_when_no_previous():
    """rls_context clears GUCs on exit when no previous value existed."""
    assert get_guc("rls.test_ctx_new") is None
    with rls_context({"rls.test_ctx_new": "temp"}):
        assert get_guc("rls.test_ctx_new") == "temp"
    assert get_guc("rls.test_ctx_new") is None


def test_rls_context_nesting():
    """Inner rls_context restores outer context's values on exit."""
    with rls_context({"rls.test_nest": "outer"}):
        assert get_guc("rls.test_nest") == "outer"
        with rls_context({"rls.test_nest": "inner"}):
            assert get_guc("rls.test_nest") == "inner"
        assert get_guc("rls.test_nest") == "outer"
    assert get_guc("rls.test_nest") is None


def test_bypass_flag_sets_and_restores():
    """bypass_flag sets flag to 'true' and restores previous value."""
    set_guc("rls.test_flag", "false")
    with bypass_flag("rls.test_flag"):
        assert get_guc("rls.test_flag") == "true"
    assert get_guc("rls.test_flag") == "false"


def test_bypass_flag_clears_when_no_previous():
    """bypass_flag clears flag on exit when no previous value existed."""
    assert get_guc("rls.test_flag_new") is None
    with bypass_flag("rls.test_flag_new"):
        assert get_guc("rls.test_flag_new") == "true"
    assert get_guc("rls.test_flag_new") is None


def test_exception_in_body_still_cleans_up():
    """The finally block runs even when an exception occurs in the body."""
    assert get_guc("rls.test_exc") is None
    with pytest.raises(ValueError, match="boom"), rls_context({"rls.test_exc": "should_cleanup"}):
        raise ValueError("boom")
    assert get_guc("rls.test_exc") is None


# ---------------------------------------------------------------------------
# is_local=True tests (transaction-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_rls_context_is_local_sets_and_auto_clears():
    """rls_context with is_local=True auto-clears on commit."""
    from django.db import transaction  # noqa: PLC0415

    with transaction.atomic(), rls_context({"rls.test_local_ctx": "local_val"}, is_local=True):
        assert get_guc("rls.test_local_ctx") == "local_val"
    # After transaction commits, SET LOCAL value is gone
    assert get_guc("rls.test_local_ctx") is None


@pytest.mark.django_db(transaction=True)
def test_bypass_flag_is_local_sets_and_auto_clears():
    """bypass_flag with is_local=True auto-clears on commit."""
    from django.db import transaction  # noqa: PLC0415

    with transaction.atomic(), bypass_flag("rls.test_local_flag", is_local=True):
        assert get_guc("rls.test_local_flag") == "true"
    assert get_guc("rls.test_local_flag") is None


@pytest.mark.django_db(transaction=True)
def test_rls_context_is_local_restored_on_block_exit():
    """rls_context with is_local=True is undone when the block exits, not at COMMIT (#94).

    Before 1.3.2 the value persisted until the end of the transaction, so an
    enclosing block silently ran with the inner scope.
    """
    from django.db import transaction  # noqa: PLC0415

    with transaction.atomic():
        with rls_context({"rls.test_local_vis": "visible"}, is_local=True):
            assert get_guc("rls.test_local_vis") == "visible"
        # Block exited, transaction still open: the value is gone already.
        assert get_guc("rls.test_local_vis") is None
    assert get_guc("rls.test_local_vis") is None


# ---------------------------------------------------------------------------
# Nested restore in both modes (#94) and aborted-transaction exits (#102)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("is_local", [False, True], ids=["set_config", "set_local"])
class TestNestedRestore:
    """Inner blocks restore the outer *database* scope in both GUC modes."""

    def test_rls_context_nested_restores_outer_value(self, is_local):
        """tenant-style nesting: the outer value is back after the inner block exits."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic(), rls_context({"rls.nest": "outer"}, is_local=is_local):
            with rls_context({"rls.nest": "inner"}, is_local=is_local):
                assert get_guc("rls.nest") == "inner"
            assert get_guc("rls.nest") == "outer"
        assert get_guc("rls.nest") is None

    def test_bypass_flag_nested_in_rls_context_is_cleared_on_exit(self, is_local):
        """The bypass flag does not outlive its block inside an outer context (#94)."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic(), rls_context({"rls.nest_t": "42"}, is_local=is_local):
            with bypass_flag("rls.nest_flag", is_local=is_local):
                assert get_guc("rls.nest_flag") == "true"
            # Asserted via raw current_setting(), not through any ORM filter.
            assert get_guc("rls.nest_flag") is None
            assert get_guc("rls.nest_t") == "42"

    def test_bypass_flag_nested_restores_previous_flag_value(self, is_local):
        """A pre-existing flag value is reinstated, not cleared."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic():
            set_guc("rls.nest_prev", "false", is_local=is_local)
            with bypass_flag("rls.nest_prev", is_local=is_local):
                assert get_guc("rls.nest_prev") == "true"
            assert get_guc("rls.nest_prev") == "false"

    @pytest.mark.parametrize("use_bypass", [False, True], ids=["rls_context", "bypass_flag"])
    def test_python_exception_still_restores_outer_scope(self, is_local, use_bypass):
        """A non-DB exception leaves the transaction healthy, so the restore must run."""
        from django.db import transaction  # noqa: PLC0415

        inner = (
            bypass_flag("rls.nest_exc", is_local=is_local)
            if use_bypass
            else rls_context({"rls.nest_exc": "inner"}, is_local=is_local)
        )
        with transaction.atomic(), rls_context({"rls.nest_exc": "outer"}, is_local=is_local):
            with pytest.raises(ValueError, match="boom"), inner:
                raise ValueError("boom")
            assert get_guc("rls.nest_exc") == "outer"

    @pytest.mark.parametrize("use_bypass", [False, True], ids=["rls_context", "bypass_flag"])
    def test_db_error_surfaces_original_exception(self, is_local, use_bypass):
        """A failed statement is reported as itself, not as 'transaction is aborted' (#102)."""
        from django.db import DataError, connection, transaction  # noqa: PLC0415

        inner = (
            bypass_flag("rls.nest_db", is_local=is_local)
            if use_bypass
            else rls_context({"rls.nest_db": "v"}, is_local=is_local)
        )
        with (
            pytest.raises(DataError, match="division by zero"),
            transaction.atomic(),
            inner,
            connection.cursor() as cur,
        ):
            cur.execute("SELECT 1/0")
        # The whole transaction rolled back, taking the GUC change with it.
        assert get_guc("rls.nest_db") is None

    @pytest.mark.parametrize("use_bypass", [False, True], ids=["rls_context", "bypass_flag"])
    def test_db_error_inside_savepoint_surfaces_original_and_rollback_restores(
        self, is_local, use_bypass
    ):
        """Same inside a nested atomic(): savepoint rollback reinstates the outer scope."""
        from django.db import DataError, connection, transaction  # noqa: PLC0415

        inner = (
            bypass_flag("rls.nest_sp", is_local=is_local)
            if use_bypass
            else rls_context({"rls.nest_sp": "inner"}, is_local=is_local)
        )
        with transaction.atomic(), rls_context({"rls.nest_sp": "outer"}, is_local=is_local):
            with (
                pytest.raises(DataError, match="division by zero"),
                transaction.atomic(),
                inner,
                connection.cursor() as cur,
            ):
                cur.execute("SELECT 1/0")
            assert get_guc("rls.nest_sp") == "outer"
            # The connection is usable again: the error did not poison the outer block.
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)

    def test_restore_failure_on_normal_exit_propagates(self, is_local, monkeypatch):
        """Only an exception exit swallows a restore error; a clean exit must not."""
        from django.db import DatabaseError, transaction  # noqa: PLC0415

        import django_rls_tenants.rls.context as ctx_mod  # noqa: PLC0415

        def _boom(*args, **kwargs):
            raise DatabaseError("restore failed")

        monkeypatch.setattr(ctx_mod, "restore_guc", _boom)
        with (
            pytest.raises(DatabaseError, match="restore failed"),
            transaction.atomic(),
            rls_context({"rls.nest_fail": "v"}, is_local=is_local),
        ):
            pass


@pytest.mark.django_db(transaction=True)
def test_partial_setup_failure_restores_previous_values():
    """A rejected later setting must not leak an earlier admin bypass in autocommit."""
    set_guc("rls.is_admin", "false")
    previous_timeout = get_guc("statement_timeout")

    with (
        pytest.raises(DataError, match="invalid value"),
        rls_context({"rls.is_admin": "true", "statement_timeout": "invalid"}),
    ):
        pytest.fail("The block must not run after setup fails")

    assert get_guc("rls.is_admin") == "false"
    assert get_guc("statement_timeout") == previous_timeout


def test_restore_runs_while_django_marks_the_transaction_for_rollback():
    """Django's rollback flag does not block the restore (#131).

    An exception leaving an inner ``atomic(savepoint=False)`` sets the flag
    while PostgreSQL's transaction is healthy. A caller that catches it and
    clears the flag commits, so a skipped restore would commit the GUC too.
    """
    with transaction.atomic():
        set_guc("rls.is_admin", "true")
        transaction.set_rollback(True)
        restore_gucs({"rls.is_admin": None}, exc_pending=True)
        assert transaction.get_rollback() is True
        transaction.set_rollback(False)
        assert get_guc("rls.is_admin") is None


def test_restore_of_nothing_touches_no_connection(monkeypatch):
    """An empty mapping returns before looking up the connection."""
    import django_rls_tenants.rls.context as ctx_mod  # noqa: PLC0415

    monkeypatch.setattr(ctx_mod, "connections", None)
    restore_gucs({}, using="nowhere")


# ---------------------------------------------------------------------------
# async_rls_context / async_bypass_flag (#37)
# ---------------------------------------------------------------------------


class TestAsyncRlsContext:
    """Async managers driven through async_to_sync (hops run on the test thread)."""

    def test_sets_and_restores(self):
        """async_rls_context sets GUCs in the block and restores previous values."""
        set_guc("rls.test_actx", "original")

        async def run():
            async with async_rls_context({"rls.test_actx": "inside"}):
                return await aget_guc("rls.test_actx")

        assert async_to_sync(run)() == "inside"
        assert get_guc("rls.test_actx") == "original"

    def test_clears_when_no_previous(self):
        """async_rls_context clears GUCs on exit when no previous value existed."""

        async def run():
            async with async_rls_context({"rls.test_actx_new": "temp"}):
                return await aget_guc("rls.test_actx_new")

        assert async_to_sync(run)() == "temp"
        assert get_guc("rls.test_actx_new") is None

    def test_nesting_restores_outer_value(self):
        """An inner async_rls_context restores the outer block's value."""

        async def run():
            seen = []
            async with async_rls_context({"rls.test_anest": "outer"}):
                async with async_rls_context({"rls.test_anest": "inner"}):
                    seen.append(await aget_guc("rls.test_anest"))
                seen.append(await aget_guc("rls.test_anest"))
            return seen

        assert async_to_sync(run)() == ["inner", "outer"]
        assert get_guc("rls.test_anest") is None

    def test_bypass_flag_sets_and_restores(self):
        """async_bypass_flag sets 'true' and restores the previous value."""
        set_guc("rls.test_aflag", "false")

        async def run():
            async with async_bypass_flag("rls.test_aflag"):
                return await aget_guc("rls.test_aflag")

        assert async_to_sync(run)() == "true"
        assert get_guc("rls.test_aflag") == "false"

    def test_bypass_flag_clears_when_no_previous(self):
        """async_bypass_flag clears the flag on exit when it was unset before."""

        async def run():
            async with async_bypass_flag("rls.test_aflag_new"):
                return await aget_guc("rls.test_aflag_new")

        assert async_to_sync(run)() == "true"
        assert get_guc("rls.test_aflag_new") is None

    @pytest.mark.parametrize("use_bypass", [False, True], ids=["rls_context", "bypass_flag"])
    def test_exception_in_body_still_restores(self, use_bypass):
        """A Python exception in the body propagates and the GUC is restored."""
        set_guc("rls.test_aexc", "outer")

        async def run():
            inner = (
                async_bypass_flag("rls.test_aexc")
                if use_bypass
                else async_rls_context({"rls.test_aexc": "inner"})
            )
            async with inner:
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            async_to_sync(run)()
        assert get_guc("rls.test_aexc") == "outer"

    def test_cancelled_error_in_body_restores(self):
        """CancelledError raised in the body still runs the restore hop (and propagates)."""

        async def run():
            async with async_rls_context({"rls.test_acancel": "v"}):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            async_to_sync(run)()
        assert get_guc("rls.test_acancel") is None

    def test_cancelled_during_enter_hop_restores(self, monkeypatch):
        """A cancel that lands while the enter hop runs still undoes the GUC change.

        asgiref cannot stop a sync function already running in its thread: the hop
        completes, its result is discarded and ``CancelledError`` is raised. The
        manager must wait for the snapshot and restore it before propagating.
        """
        import django_rls_tenants.rls.context as ctx_mod  # noqa: PLC0415

        real_enter = ctx_mod._enter_gucs
        holder = {}

        def slow_enter(variables, **kwargs):
            # Cancel the awaiting task from inside the hop, then keep the hop busy
            # long enough for the cancellation to be delivered while it runs.
            holder["loop"].call_soon_threadsafe(holder["task"].cancel)
            time.sleep(0.05)
            return real_enter(variables, **kwargs)

        monkeypatch.setattr(ctx_mod, "_enter_gucs", slow_enter)

        async def body():
            async with async_rls_context({"rls.test_acancel_enter": "v"}):
                pytest.fail("The block must not run after a cancelled entry")

        async def run():
            holder["loop"] = asyncio.get_running_loop()
            holder["task"] = asyncio.create_task(body())
            with pytest.raises(asyncio.CancelledError):
                await holder["task"]
            return await aget_guc("rls.test_acancel_enter")

        assert async_to_sync(run)() is None
        assert get_guc("rls.test_acancel_enter") is None

    def test_sync_rls_context_inside_hop_inside_async_block(self):
        """A sync rls_context run in a hop nests inside an async block and restores it."""

        def sync_inner():
            with rls_context({"rls.test_amix": "sync_inner"}):
                return get_guc("rls.test_amix")

        async def run():
            async with async_rls_context({"rls.test_amix": "async_outer"}):
                inner = await sync_to_async(sync_inner, thread_sensitive=True)()
                return inner, await aget_guc("rls.test_amix")

        assert async_to_sync(run)() == ("sync_inner", "async_outer")
        assert get_guc("rls.test_amix") is None

    def test_async_block_inside_sync_rls_context(self):
        """An async block run through async_to_sync inside a sync block restores it."""

        async def async_inner():
            async with async_rls_context({"rls.test_amix2": "async_inner"}):
                return await aget_guc("rls.test_amix2")

        with rls_context({"rls.test_amix2": "sync_outer"}):
            assert async_to_sync(async_inner)() == "async_inner"
            assert get_guc("rls.test_amix2") == "sync_outer"
        assert get_guc("rls.test_amix2") is None


class TestEnterGucs:
    """The private one-hop enter helper used by the async managers."""

    def test_returns_previous_values(self):
        """The snapshot is taken before the variables are set."""
        set_guc("rls.test_enter_a", "before")
        previous = _enter_gucs(
            {"rls.test_enter_a": "after", "rls.test_enter_b": "x"},
            is_local=False,
            using="default",
        )
        assert previous == {"rls.test_enter_a": "before", "rls.test_enter_b": None}
        assert get_guc("rls.test_enter_a") == "after"
        assert get_guc("rls.test_enter_b") == "x"

    def test_partial_failure_restores_already_set(self, monkeypatch):
        """If the second set fails, the first variable is restored before the error."""
        import django_rls_tenants.rls.context as ctx_mod  # noqa: PLC0415

        set_guc("rls.test_enter_first", "before")
        real_set_guc = ctx_mod.set_guc

        def _fail_second(name, value, **kwargs):
            if name == "rls.test_enter_second":
                raise RuntimeError("second failed")
            real_set_guc(name, value, **kwargs)

        monkeypatch.setattr(ctx_mod, "set_guc", _fail_second)
        with pytest.raises(RuntimeError, match="second failed"):
            _enter_gucs(
                {"rls.test_enter_first": "changed", "rls.test_enter_second": "v"},
                is_local=False,
                using="default",
            )
        assert get_guc("rls.test_enter_first") == "before"
        assert get_guc("rls.test_enter_second") is None


@pytest.mark.django_db(transaction=True)
def test_async_partial_setup_failure_restores_previous_values():
    """A rejected later setting must not leak an earlier admin bypass (async form)."""
    set_guc("rls.is_admin", "false")
    previous_timeout = get_guc("statement_timeout")

    async def run():
        async with async_rls_context({"rls.is_admin": "true", "statement_timeout": "invalid"}):
            pytest.fail("The block must not run after setup fails")

    with pytest.raises(DataError, match="invalid value"):
        async_to_sync(run)()

    assert get_guc("rls.is_admin") == "false"
    assert get_guc("statement_timeout") == previous_timeout


def _divide_by_zero():
    from django.db import connection  # noqa: PLC0415

    with connection.cursor() as cur:
        cur.execute("SELECT 1/0")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("is_local", [False, True], ids=["set_config", "set_local"])
class TestAsyncNestedRestore:
    """Async blocks restore the outer *database* scope in both GUC modes.

    ``SET LOCAL`` from a coroutine needs ``atomic()`` open on the hop thread;
    under ``async_to_sync`` that is the test thread, so each test opens the
    transaction in sync code and drives the coroutine from inside it.
    """

    def test_nested_restores_outer_value(self, is_local):
        """Inner async block restores the outer async block's value."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            seen = []
            async with async_rls_context({"rls.anest": "outer"}, is_local=is_local):
                async with async_rls_context({"rls.anest": "inner"}, is_local=is_local):
                    seen.append(await aget_guc("rls.anest"))
                seen.append(await aget_guc("rls.anest"))
            return seen

        with transaction.atomic():
            assert async_to_sync(run)() == ["inner", "outer"]
            assert get_guc("rls.anest") is None
        assert get_guc("rls.anest") is None

    def test_bypass_flag_nested_is_cleared_on_exit(self, is_local):
        """The async bypass flag does not outlive its block inside an outer context."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            async with async_rls_context({"rls.anest_t": "42"}, is_local=is_local):
                async with async_bypass_flag("rls.anest_flag", is_local=is_local):
                    inside = await aget_guc("rls.anest_flag")
                return inside, await aget_guc("rls.anest_flag"), await aget_guc("rls.anest_t")

        with transaction.atomic():
            assert async_to_sync(run)() == ("true", None, "42")

    @pytest.mark.parametrize("use_bypass", [False, True], ids=["rls_context", "bypass_flag"])
    def test_db_error_surfaces_original_exception(self, is_local, use_bypass):
        """A failed statement is reported as itself, not as 'transaction is aborted'."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            inner = (
                async_bypass_flag("rls.anest_db", is_local=is_local)
                if use_bypass
                else async_rls_context({"rls.anest_db": "v"}, is_local=is_local)
            )
            async with inner:
                await sync_to_async(_divide_by_zero, thread_sensitive=True)()

        with pytest.raises(DataError, match="division by zero"), transaction.atomic():
            async_to_sync(run)()
        # The whole transaction rolled back, taking the GUC change with it.
        assert get_guc("rls.anest_db") is None

    def test_db_error_inside_savepoint_rollback_restores_outer(self, is_local):
        """Inside a nested atomic(): the savepoint rollback reinstates the outer scope."""
        from django.db import connection, transaction  # noqa: PLC0415

        async def inner():
            async with async_rls_context({"rls.anest_sp": "inner"}, is_local=is_local):
                await sync_to_async(_divide_by_zero, thread_sensitive=True)()

        with transaction.atomic(), rls_context({"rls.anest_sp": "outer"}, is_local=is_local):
            with pytest.raises(DataError, match="division by zero"), transaction.atomic():
                async_to_sync(inner)()
            assert get_guc("rls.anest_sp") == "outer"
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)


@pytest.mark.django_db(transaction=True)
def test_async_rls_context_is_local_without_transaction_raises():
    """The generic manager lets set_guc's RuntimeError propagate for is_local=True."""

    async def run():
        async with async_rls_context({"rls.test_alocal": "v"}, is_local=True):
            pytest.fail("The block must not run without a transaction")

    with pytest.raises(RuntimeError, match="Cannot use SET LOCAL"):
        async_to_sync(run)()
    assert get_guc("rls.test_alocal") is None
