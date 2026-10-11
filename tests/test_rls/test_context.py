"""Tests for django_rls_tenants.rls.context."""

from __future__ import annotations

import pytest
from django.db import DataError

from django_rls_tenants.rls.context import bypass_flag, rls_context
from django_rls_tenants.rls.guc import get_guc, set_guc

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
