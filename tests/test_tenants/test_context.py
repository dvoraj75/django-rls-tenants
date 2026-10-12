"""Tests for django_rls_tenants.tenants.context."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.rls.guc import aget_guc, get_guc, set_guc
from django_rls_tenants.tenants.context import (
    _resolve_user_guc_vars,
    admin_context,
    async_admin_context,
    async_tenant_context,
    tenant_context,
    with_rls_context,
)
from django_rls_tenants.tenants.errors import HINT_TENANT_ID_NONE
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active

pytestmark = pytest.mark.django_db


class TestTenantContext:
    """Tests for tenant_context()."""

    def test_sets_gucs(self, tenant_a):
        """Sets is_admin=false and current_tenant=id."""
        with tenant_context(tenant_a.pk):
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_clears_on_exit(self, tenant_a):
        """Clears GUCs after the context exits."""
        with tenant_context(tenant_a.pk):
            pass
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_restores_previous(self, tenant_a):
        """Restores previous GUC values on exit (nesting support)."""
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "999")
        with tenant_context(tenant_a.pk):
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)
        assert get_guc("rls.is_admin") == "true"
        assert get_guc("rls.current_tenant") == "999"

    def test_none_raises_no_tenant_context_error(self):
        """tenant_context(None) raises NoTenantContextError."""
        with pytest.raises(NoTenantContextError, match="tenant_id cannot be None"):  # noqa: SIM117
            with tenant_context(None):
                pass

    def test_none_error_carries_hint(self):
        """tenant_context(None) attaches an actionable hint pointing to admin_context()."""
        with pytest.raises(NoTenantContextError) as exc_info:  # noqa: SIM117
            with tenant_context(None):
                pass
        assert exc_info.value.hint is not None
        assert "admin_context()" in exc_info.value.hint

    def test_string_tenant_id(self):
        """Works with string tenant IDs (e.g., UUIDs)."""
        with tenant_context("abc-123"):
            assert get_guc("rls.current_tenant") == "abc-123"

    def test_sets_tenant_state(self, tenant_a):
        """Sets ContextVar tenant state for auto-scoping."""
        assert get_current_tenant_id() is None
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
        assert get_current_tenant_id() is None

    def test_restores_state_on_exit(self, tenant_a):
        """ContextVar state is restored on exit even after nesting."""
        with tenant_context(1):
            assert get_current_tenant_id() == 1
            with tenant_context(tenant_a.pk):
                assert get_current_tenant_id() == tenant_a.pk
            assert get_current_tenant_id() == 1
        assert get_current_tenant_id() is None

    def test_state_cleared_on_exception(self, tenant_a):
        """ContextVar state is restored when an exception occurs."""
        with pytest.raises(RuntimeError, match="boom"), tenant_context(tenant_a.pk):
            raise RuntimeError("boom")
        assert get_current_tenant_id() is None


class TestAdminContext:
    """Tests for admin_context()."""

    def test_sets_gucs(self):
        """Sets is_admin=true and clears current_tenant."""
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
            # Admin mode clears tenant GUC; admin_bypass clause handles access.
            assert get_guc("rls.current_tenant") is None

    def test_clears_on_exit(self):
        """Clears GUCs after the context exits."""
        with admin_context():
            pass
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_restores_previous(self, tenant_a):
        """Restores previous GUC values on exit."""
        set_guc("rls.is_admin", "false")
        set_guc("rls.current_tenant", str(tenant_a.pk))
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
        assert get_guc("rls.is_admin") == "false"
        assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_clears_tenant_state(self):
        """Admin context clears ContextVar tenant state (no auto-scope filter)."""
        assert get_current_tenant_id() is None
        with admin_context():
            assert get_current_tenant_id() is None
        assert get_current_tenant_id() is None

    def test_clears_state_inside_tenant(self, tenant_a):
        """Admin context inside tenant context clears and restores state."""
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
            with admin_context():
                assert get_current_tenant_id() is None
            assert get_current_tenant_id() == tenant_a.pk


class TestNesting:
    """Tests for nesting tenant_context and admin_context."""

    def test_admin_inside_tenant(self, tenant_a):
        """Inner admin_context restores outer tenant_context on exit."""
        with tenant_context(tenant_a.pk):
            assert get_guc("rls.is_admin") == "false"
            with admin_context():
                assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_tenant_inside_admin(self, tenant_a):
        """Inner tenant_context restores outer admin_context on exit."""
        with admin_context():
            assert get_guc("rls.is_admin") == "true"
            with tenant_context(tenant_a.pk):
                assert get_guc("rls.is_admin") == "false"
                assert get_guc("rls.current_tenant") == str(tenant_a.pk)
            assert get_guc("rls.is_admin") == "true"
            # Admin mode clears tenant GUC; admin_bypass handles access.
            assert get_guc("rls.current_tenant") is None

    def test_exception_in_body_cleans_up(self, tenant_a):
        """GUCs are restored even when an exception occurs in the block."""
        with pytest.raises(RuntimeError, match="boom"), tenant_context(tenant_a.pk):
            raise RuntimeError("boom")
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_state_nesting_admin_inside_tenant(self, tenant_a):
        """ContextVar state tracks nesting of admin inside tenant."""
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
            with admin_context():
                assert get_current_tenant_id() is None
            assert get_current_tenant_id() == tenant_a.pk
        assert get_current_tenant_id() is None

    def test_state_nesting_tenant_inside_admin(self, tenant_a):
        """ContextVar state tracks nesting of tenant inside admin."""
        with admin_context():
            assert get_current_tenant_id() is None
            with tenant_context(tenant_a.pk):
                assert get_current_tenant_id() == tenant_a.pk
            assert get_current_tenant_id() is None
        assert get_current_tenant_id() is None

    def test_state_nesting_tenant_inside_tenant(self, tenant_a, tenant_b):
        """ContextVar state tracks nesting of two different tenant contexts."""
        with tenant_context(tenant_a.pk):
            assert get_current_tenant_id() == tenant_a.pk
            with tenant_context(tenant_b.pk):
                assert get_current_tenant_id() == tenant_b.pk
            assert get_current_tenant_id() == tenant_a.pk
        assert get_current_tenant_id() is None


class TestResolveUserGucVars:
    """Tests for _resolve_user_guc_vars()."""

    def test_admin_user_returns_admin_gucs(self, admin_user):
        """Admin user returns is_admin=true and empty tenant."""
        result = _resolve_user_guc_vars(admin_user)
        assert result["rls.is_admin"] == "true"
        assert result["rls.current_tenant"] == ""

    def test_tenant_user_returns_tenant_gucs(self, tenant_a_user):
        """Tenant user returns is_admin=false and tenant ID string."""
        result = _resolve_user_guc_vars(tenant_a_user)
        assert result["rls.is_admin"] == "false"
        assert result["rls.current_tenant"] == str(tenant_a_user.rls_tenant_id)

    def test_non_admin_with_none_tenant_id_raises(self):
        """Non-admin user with rls_tenant_id=None raises NoTenantContextError."""
        user = MagicMock()
        user.is_tenant_admin = False
        user.rls_tenant_id = None
        with pytest.raises(NoTenantContextError, match="rls_tenant_id=None"):
            _resolve_user_guc_vars(user)

    def test_non_admin_none_tenant_id_error_carries_hint(self):
        """The error tells the user to assign a tenant or set is_tenant_admin=True."""
        user = MagicMock()
        user.is_tenant_admin = False
        user.rls_tenant_id = None
        with pytest.raises(NoTenantContextError) as exc_info:
            _resolve_user_guc_vars(user)
        assert exc_info.value.hint is not None
        assert "is_tenant_admin=True" in exc_info.value.hint

    def test_admin_with_none_tenant_id_ok(self):
        """Admin user with rls_tenant_id=None is valid (admin bypasses RLS)."""
        user = MagicMock()
        user.is_tenant_admin = True
        user.rls_tenant_id = None
        result = _resolve_user_guc_vars(user)
        assert result["rls.is_admin"] == "true"
        assert result["rls.current_tenant"] == ""


class TestWithRlsContext:
    """Tests for the @with_rls_context decorator."""

    def test_admin_user(self, admin_user):
        """Decorator sets admin context for admin user."""

        @with_rls_context
        def my_func(as_user):
            return get_guc("rls.is_admin")

        result = my_func(as_user=admin_user)
        assert result == "true"

    def test_tenant_user(self, tenant_a_user):
        """Decorator sets tenant context for tenant user."""

        @with_rls_context
        def my_func(as_user):
            return get_guc("rls.current_tenant")

        result = my_func(as_user=tenant_a_user)
        assert result == str(tenant_a_user.rls_tenant_id)

    def test_none_user_warns(self, caplog):
        """Decorator logs warning when as_user=None."""

        @with_rls_context
        def my_func(as_user):
            return "called"

        with caplog.at_level(logging.WARNING, logger="django_rls_tenants"):
            result = my_func(as_user=None)
        assert result == "called"
        assert "as_user is None" in caplog.text

    def test_none_user_no_context(self):
        """No RLS context set when as_user=None (fail-closed)."""

        @with_rls_context
        def my_func(as_user):
            return get_guc("rls.is_admin")

        result = my_func(as_user=None)
        assert result is None

    def test_positional_arg(self, tenant_a_user):
        """Works when as_user is passed as a positional argument."""

        @with_rls_context
        def my_func(as_user, extra=None):
            return get_guc("rls.current_tenant")

        result = my_func(tenant_a_user)
        assert result == str(tenant_a_user.rls_tenant_id)

    def test_keyword_arg(self, tenant_a_user):
        """Works when as_user is passed as a keyword argument."""

        @with_rls_context
        def my_func(other_arg, as_user=None):
            return get_guc("rls.current_tenant")

        result = my_func("something", as_user=tenant_a_user)
        assert result == str(tenant_a_user.rls_tenant_id)

    def test_clears_after_return(self, tenant_a_user):
        """GUCs are cleared after the decorated function returns."""

        @with_rls_context
        def my_func(as_user):
            return "done"

        my_func(as_user=tenant_a_user)
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None

    def test_non_admin_with_none_tenant_id_raises(self):
        """Decorator raises NoTenantContextError for non-admin user with rls_tenant_id=None."""
        user = MagicMock()
        user.is_tenant_admin = False
        user.rls_tenant_id = None

        @with_rls_context
        def my_func(as_user):
            return "should not reach"

        with pytest.raises(NoTenantContextError, match="rls_tenant_id=None"):
            my_func(as_user=user)

    def test_non_admin_none_tenant_id_error_carries_hint(self):
        """The decorator's NoTenantContextError carries the assign-a-tenant hint."""
        user = MagicMock()
        user.is_tenant_admin = False
        user.rls_tenant_id = None

        @with_rls_context
        def my_func(as_user):
            return "should not reach"

        with pytest.raises(NoTenantContextError) as exc_info:
            my_func(as_user=user)
        assert exc_info.value.hint is not None
        assert "is_tenant_admin=True" in exc_info.value.hint


class TestIsLocalMode:
    """Tests for USE_LOCAL_SET=True (transaction-scoped GUCs)."""

    @pytest.mark.django_db(transaction=True)
    def test_tenant_context_is_local(self, tenant_a):
        """tenant_context with USE_LOCAL_SET=True auto-clears on commit."""
        from django.db import transaction  # noqa: PLC0415
        from django.test import override_settings  # noqa: PLC0415

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            }
        ):
            with transaction.atomic(), tenant_context(tenant_a.pk):
                assert get_guc("rls.is_admin") == "false"
                assert get_guc("rls.current_tenant") == str(tenant_a.pk)
            # After transaction, SET LOCAL values are gone
            assert get_guc("rls.is_admin") is None
            assert get_guc("rls.current_tenant") is None

    @pytest.mark.django_db(transaction=True)
    def test_admin_context_is_local(self):
        """admin_context with USE_LOCAL_SET=True auto-clears on commit."""
        from django.db import transaction  # noqa: PLC0415
        from django.test import override_settings  # noqa: PLC0415

        with override_settings(
            RLS_TENANTS={
                "TENANT_MODEL": "test_app.Tenant",
                "USE_LOCAL_SET": True,
            }
        ):
            with transaction.atomic(), admin_context():
                assert get_guc("rls.is_admin") == "true"
            # After transaction, SET LOCAL values are gone
            assert get_guc("rls.is_admin") is None


@pytest.fixture(params=[False, True], ids=["set_config", "set_local"])
def guc_mode(request):
    """Run a test under both GUC modes; the config reloads on ``setting_changed``."""
    from django.test import override_settings  # noqa: PLC0415

    with override_settings(
        RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": request.param},
    ):
        yield request.param


@pytest.mark.django_db(transaction=True)
class TestNestedContextRestore:
    """Nested blocks restore the outer *database* scope in both modes (#94, #102).

    Every assertion reads the GUC with ``current_setting()`` (``get_guc``),
    never through ``RLSManager``, because the ORM auto-scope filter would hide
    a leaked database scope.
    """

    def test_tenant_in_tenant_restores_outer_tenant(self, guc_mode):
        """tenant -> tenant: the outer tenant id is back after the inner block."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic(), tenant_context(101):
            with tenant_context(202):
                assert get_guc("rls.current_tenant") == "202"
            assert get_guc("rls.current_tenant") == "101"
            assert get_guc("rls.is_admin") == "false"
            assert get_current_tenant_id() == 101

    def test_admin_in_tenant_does_not_leak_bypass(self, guc_mode):
        """tenant -> admin: the admin bypass must not outlive its block (the #94 leak)."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic(), tenant_context(101):
            with admin_context():
                assert get_guc("rls.is_admin") == "true"
                assert get_guc("rls.current_tenant") is None
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == "101"
            assert get_current_tenant_id() == 101

    def test_tenant_in_admin_restores_admin(self, guc_mode):
        """admin -> tenant: admin scope is reinstated after the inner tenant block."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic(), admin_context():
            with tenant_context(303):
                assert get_guc("rls.is_admin") == "false"
                assert get_guc("rls.current_tenant") == "303"
            assert get_guc("rls.is_admin") == "true"
            assert get_guc("rls.current_tenant") is None
            assert get_current_tenant_id() is None

    def test_outermost_block_clears_everything(self, guc_mode):
        """With nothing set before, exiting the outermost block clears both GUCs."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic():
            with tenant_context(101):
                pass
            assert get_guc("rls.is_admin") is None
            assert get_guc("rls.current_tenant") is None

    def test_python_exception_restores_outer_scope(self, guc_mode):
        """A non-DB exception leaves the transaction healthy; the outer scope is restored."""
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic(), tenant_context(101):
            with pytest.raises(ValueError, match="boom"), admin_context():
                raise ValueError("boom")
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == "101"

    def test_db_error_surfaces_original_exception(self, guc_mode):
        """A failed statement is reported as itself, not as 'transaction is aborted' (#102)."""
        from django.db import DataError, connection, transaction  # noqa: PLC0415

        with (
            pytest.raises(DataError, match="division by zero"),
            transaction.atomic(),
            tenant_context(101),
            connection.cursor() as cur,
        ):
            cur.execute("SELECT 1/0")
        assert get_guc("rls.current_tenant") is None
        assert get_current_tenant_id() is None

    def test_db_error_inside_savepoint_surfaces_original_and_rollback_restores(self, guc_mode):
        """Inside a nested atomic(): the savepoint rollback reinstates the outer scope."""
        from django.db import DataError, connection, transaction  # noqa: PLC0415

        with transaction.atomic(), tenant_context(101):
            with (
                pytest.raises(DataError, match="division by zero"),
                transaction.atomic(),
                admin_context(),
                connection.cursor() as cur,
            ):
                cur.execute("SELECT 1/0")
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == "101"
            assert get_current_tenant_id() == 101

    def test_integrity_error_is_not_masked(self, guc_mode, tenant_a):
        """The realistic case: a constraint violation inside a tenant block stays visible."""
        from django.db import IntegrityError, transaction  # noqa: PLC0415

        from tests.test_app.models import Tenant  # noqa: PLC0415

        with (
            pytest.raises(IntegrityError),
            transaction.atomic(),
            tenant_context(tenant_a.pk),
        ):
            Tenant.objects.create(pk=tenant_a.pk, name="duplicate pk")


# ---------------------------------------------------------------------------
# async_tenant_context / async_admin_context (#37)
# ---------------------------------------------------------------------------


async def _gucs():
    """Read both tenant GUCs from a coroutine (one hop each)."""
    return await aget_guc("rls.is_admin"), await aget_guc("rls.current_tenant")


class TestAsyncTenantContext:
    """Tests for async_tenant_context(), driven through async_to_sync."""

    def test_sets_gucs(self, tenant_a):
        """Sets is_admin=false and current_tenant=id."""

        async def run():
            async with async_tenant_context(tenant_a.pk):
                return await _gucs()

        assert async_to_sync(run)() == ("false", str(tenant_a.pk))

    def test_clears_on_exit(self, tenant_a):
        """Clears GUCs after the block exits."""

        async def run():
            async with async_tenant_context(tenant_a.pk):
                pass

        async_to_sync(run)()
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_restores_previous(self, tenant_a):
        """Restores the previous GUC values on exit."""
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "999")

        async def run():
            async with async_tenant_context(tenant_a.pk):
                return await _gucs()

        assert async_to_sync(run)() == ("false", str(tenant_a.pk))
        assert get_guc("rls.is_admin") == "true"
        assert get_guc("rls.current_tenant") == "999"

    def test_none_raises_before_any_hop(self, monkeypatch):
        """async_tenant_context(None) raises NoTenantContextError with the hint, no DB call."""
        import django_rls_tenants.tenants.context as ctx_mod  # noqa: PLC0415

        def _no_hop(*args, **kwargs):
            pytest.fail("No GUC hop may run for tenant_id=None")

        monkeypatch.setattr(ctx_mod, "_enter_tenant_gucs", _no_hop)

        async def run():
            async with async_tenant_context(None):
                pass

        with pytest.raises(NoTenantContextError, match="tenant_id cannot be None") as exc_info:
            async_to_sync(run)()
        assert exc_info.value.hint == HINT_TENANT_ID_NONE

    def test_string_tenant_id(self):
        """Works with string tenant IDs (e.g., UUIDs)."""

        async def run():
            async with async_tenant_context("abc-123"):
                return await aget_guc("rls.current_tenant")

        assert async_to_sync(run)() == "abc-123"

    def test_sets_state_in_coroutine_and_hops(self, tenant_a):
        """ContextVar state is set in the coroutine, visible in hops, and reset on exit."""

        async def run():
            before = (get_current_tenant_id(), get_rls_context_active())
            async with async_tenant_context(tenant_a.pk):
                inside = (get_current_tenant_id(), get_rls_context_active())
                in_hop = await sync_to_async(get_current_tenant_id, thread_sensitive=True)()
            after = (get_current_tenant_id(), get_rls_context_active())
            return before, inside, in_hop, after

        before, inside, in_hop, after = async_to_sync(run)()
        assert before == (None, False)
        assert inside == (tenant_a.pk, True)
        assert in_hop == tenant_a.pk
        assert after == (None, False)
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    def test_state_cleared_on_exception(self, tenant_a):
        """ContextVar state and GUCs are restored when the body raises."""

        async def run():
            async with async_tenant_context(tenant_a.pk):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            async_to_sync(run)()
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False
        assert get_guc("rls.current_tenant") is None

    def test_logs_enter_and_exit(self, tenant_a, caplog):
        """Entry and exit are logged at DEBUG like the sync manager."""

        async def run():
            async with async_tenant_context(tenant_a.pk):
                pass

        with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
            async_to_sync(run)()
        assert "async_tenant_context entered" in caplog.text
        assert "async_tenant_context exited" in caplog.text


class TestAsyncAdminContext:
    """Tests for async_admin_context(), driven through async_to_sync."""

    def test_sets_gucs(self):
        """Sets is_admin=true and clears current_tenant."""

        async def run():
            async with async_admin_context():
                return await _gucs(), get_current_tenant_id(), get_rls_context_active()

        assert async_to_sync(run)() == (("true", None), None, True)
        assert get_guc("rls.is_admin") is None
        assert get_rls_context_active() is False

    def test_clears_state_inside_async_tenant(self, tenant_a):
        """async_admin_context inside async_tenant_context clears and restores state."""

        async def run():
            seen = []
            async with async_tenant_context(tenant_a.pk):
                seen.append(get_current_tenant_id())
                async with async_admin_context():
                    seen.append(get_current_tenant_id())
                seen.append(get_current_tenant_id())
            return seen

        assert async_to_sync(run)() == [tenant_a.pk, None, tenant_a.pk]

    def test_restores_previous(self, tenant_a):
        """Restores previous GUC values on exit."""
        set_guc("rls.is_admin", "false")
        set_guc("rls.current_tenant", str(tenant_a.pk))

        async def run():
            async with async_admin_context():
                return await aget_guc("rls.is_admin")

        assert async_to_sync(run)() == "true"
        assert get_guc("rls.is_admin") == "false"
        assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_logs_enter_and_exit(self, caplog):
        """Entry and exit are logged at DEBUG like the sync manager."""

        async def run():
            async with async_admin_context():
                pass

        with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
            async_to_sync(run)()
        assert "async_admin_context entered" in caplog.text
        assert "async_admin_context exited" in caplog.text


class TestAsyncNesting:
    """Nesting of async managers with each other and with the sync managers."""

    def test_admin_inside_tenant_does_not_leak_bypass(self, tenant_a):
        """The inner admin bypass is gone after its block (read via get_guc, not the ORM)."""

        async def run():
            async with async_tenant_context(tenant_a.pk):
                async with async_admin_context():
                    inner = await _gucs()
                return inner, await _gucs()

        inner, outer = async_to_sync(run)()
        assert inner == ("true", None)
        assert outer == ("false", str(tenant_a.pk))

    def test_tenant_inside_admin(self, tenant_a):
        """Inner async tenant block restores the outer admin scope."""

        async def run():
            async with async_admin_context():
                async with async_tenant_context(tenant_a.pk):
                    inner = await _gucs()
                return inner, await _gucs(), get_current_tenant_id()

        assert async_to_sync(run)() == (("false", str(tenant_a.pk)), ("true", None), None)

    def test_tenant_inside_tenant(self, tenant_a, tenant_b):
        """Two async tenant blocks nest; ContextVar and GUC both return to the outer."""

        async def run():
            seen = []
            async with async_tenant_context(tenant_a.pk):
                async with async_tenant_context(tenant_b.pk):
                    seen.append((get_current_tenant_id(), await aget_guc("rls.current_tenant")))
                seen.append((get_current_tenant_id(), await aget_guc("rls.current_tenant")))
            return seen

        assert async_to_sync(run)() == [
            (tenant_b.pk, str(tenant_b.pk)),
            (tenant_a.pk, str(tenant_a.pk)),
        ]
        assert get_current_tenant_id() is None

    def test_sync_tenant_context_in_hop_inside_async_block(self, tenant_a, tenant_b):
        """A sync tenant_context in a hop nests inside an async block and leaves no state."""

        def sync_inner():
            with tenant_context(tenant_b.pk):
                return get_current_tenant_id(), get_guc("rls.current_tenant")

        async def run():
            async with async_tenant_context(tenant_a.pk):
                inner = await sync_to_async(sync_inner, thread_sensitive=True)()
                return inner, get_current_tenant_id(), await aget_guc("rls.current_tenant")

        inner, state_after, guc_after = async_to_sync(run)()
        assert inner == (tenant_b.pk, str(tenant_b.pk))
        assert state_after == tenant_a.pk
        assert guc_after == str(tenant_a.pk)

    def test_async_block_inside_sync_tenant_context(self, tenant_a, tenant_b):
        """An async block run via async_to_sync inside a sync block restores the sync scope."""

        async def async_inner():
            async with async_tenant_context(tenant_b.pk):
                return get_current_tenant_id(), await aget_guc("rls.current_tenant")

        with tenant_context(tenant_a.pk):
            assert async_to_sync(async_inner)() == (tenant_b.pk, str(tenant_b.pk))
            assert get_current_tenant_id() == tenant_a.pk
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)
            assert get_guc("rls.is_admin") == "false"


def _divide_by_zero():
    from django.db import connection  # noqa: PLC0415

    with connection.cursor() as cur:
        cur.execute("SELECT 1/0")


@pytest.mark.django_db(transaction=True)
class TestAsyncNestedContextRestore:
    """Async nested blocks restore the outer *database* scope in both modes.

    The coroutine is driven through ``async_to_sync`` from inside
    ``transaction.atomic()``, the supported shape for ``USE_LOCAL_SET=True``.
    Every GUC is read with ``current_setting()``, never through the ORM.
    """

    def test_tenant_in_tenant_restores_outer_tenant(self, guc_mode):
        """tenant -> tenant: the outer tenant id is back after the inner block."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            async with async_tenant_context(101):
                async with async_tenant_context(202):
                    inner = await aget_guc("rls.current_tenant")
                return inner, await _gucs(), get_current_tenant_id()

        with transaction.atomic():
            assert async_to_sync(run)() == ("202", ("false", "101"), 101)

    def test_admin_in_tenant_does_not_leak_bypass(self, guc_mode):
        """tenant -> admin: the admin bypass must not outlive its block (#94 class)."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            async with async_tenant_context(101):
                async with async_admin_context():
                    inner = await _gucs()
                return inner, await _gucs()

        with transaction.atomic():
            assert async_to_sync(run)() == (("true", None), ("false", "101"))

    def test_tenant_in_admin_restores_admin(self, guc_mode):
        """admin -> tenant: admin scope is reinstated after the inner tenant block."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            async with async_admin_context():
                async with async_tenant_context(303):
                    inner = await _gucs()
                return inner, await _gucs()

        with transaction.atomic():
            assert async_to_sync(run)() == (("false", "303"), ("true", None))

    def test_outermost_block_clears_everything(self, guc_mode):
        """With nothing set before, exiting the outermost block clears both GUCs."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            async with async_tenant_context(101):
                pass
            return await _gucs()

        with transaction.atomic():
            assert async_to_sync(run)() == (None, None)

    def test_python_exception_restores_outer_scope(self, guc_mode):
        """A non-DB exception leaves the transaction healthy; the outer scope is restored."""
        from django.db import transaction  # noqa: PLC0415

        async def run():
            async with async_tenant_context(101):
                with pytest.raises(ValueError, match="boom"):
                    async with async_admin_context():
                        raise ValueError("boom")
                return await _gucs()

        with transaction.atomic():
            assert async_to_sync(run)() == ("false", "101")

    def test_db_error_surfaces_original_exception(self, guc_mode):
        """A failed statement is reported as itself, not as 'transaction is aborted'."""
        from django.db import DataError, transaction  # noqa: PLC0415

        async def run():
            async with async_tenant_context(101):
                await sync_to_async(_divide_by_zero, thread_sensitive=True)()

        with pytest.raises(DataError, match="division by zero"), transaction.atomic():
            async_to_sync(run)()
        assert get_guc("rls.current_tenant") is None
        assert get_current_tenant_id() is None

    def test_db_error_inside_savepoint_rollback_restores(self, guc_mode):
        """Inside a nested atomic(): the savepoint rollback reinstates the outer scope."""
        from django.db import DataError, transaction  # noqa: PLC0415

        async def inner():
            async with async_admin_context():
                await sync_to_async(_divide_by_zero, thread_sensitive=True)()

        with transaction.atomic(), tenant_context(101):
            with pytest.raises(DataError, match="division by zero"), transaction.atomic():
                async_to_sync(inner)()
            assert get_guc("rls.is_admin") == "false"
            assert get_guc("rls.current_tenant") == "101"
            assert get_current_tenant_id() == 101


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("use_admin", [False, True], ids=["tenant", "admin"])
def test_use_local_set_without_transaction_raises_configuration_error(use_admin):
    """USE_LOCAL_SET=True with no transaction on the hop thread is a configuration error."""
    from django.db import connections  # noqa: PLC0415
    from django.test import override_settings  # noqa: PLC0415

    async def run():
        try:
            ctx = async_admin_context() if use_admin else async_tenant_context(1)
            with pytest.raises(RLSConfigurationError, match="USE_LOCAL_SET"):
                async with ctx:
                    pytest.fail("The block must not run")
            return get_current_tenant_id(), get_rls_context_active()
        finally:
            await sync_to_async(connections.close_all, thread_sensitive=True)()

    with override_settings(
        RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True},
    ):
        # asyncio.run: the hop runs on an executor thread outside any transaction.
        assert asyncio.run(run()) == (None, False)
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


@pytest.mark.django_db(transaction=True)
def test_use_local_set_via_async_to_sync_inside_atomic(tenant_a):
    """USE_LOCAL_SET=True works when the coroutine runs inside a sync atomic() block."""
    from django.db import transaction  # noqa: PLC0415
    from django.test import override_settings  # noqa: PLC0415

    async def run():
        async with async_tenant_context(tenant_a.pk):
            return await _gucs()

    with override_settings(
        RLS_TENANTS={"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True},
    ):
        with transaction.atomic():
            assert async_to_sync(run)() == ("false", str(tenant_a.pk))
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None


def test_async_orm_is_scoped_and_strict_mode_applies(tenant_a, tenant_b, settings):
    """The async ORM honours the coroutine's tenant state and STRICT_MODE."""
    from tests.test_app.models import Order  # noqa: PLC0415

    Order.objects.create(tenant=tenant_a, product="A", amount=1)
    Order.objects.create(tenant=tenant_b, product="B", amount=2)

    async def scoped_count():
        async with async_tenant_context(tenant_a.pk):
            return await Order.objects.acount()

    assert async_to_sync(scoped_count)() == 1

    settings.RLS_TENANTS = {"TENANT_MODEL": "test_app.Tenant", "STRICT_MODE": True}

    async def unscoped_count():
        return await Order.objects.acount()

    with pytest.raises(NoTenantContextError):
        async_to_sync(unscoped_count)()

    async def admin_count():
        async with async_admin_context():
            return await Order.objects.acount()

    assert async_to_sync(admin_count)() == 2


def test_cancelled_task_restores_gucs(tenant_a):
    """A real task.cancel() while awaiting inside the block still restores the GUCs."""
    entered = None

    async def body():
        async with async_tenant_context(tenant_a.pk):
            entered.set()
            await asyncio.sleep(10)

    async def run():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(body())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await _gucs(), get_current_tenant_id()

    assert async_to_sync(run)() == ((None, None), None)
    assert get_guc("rls.current_tenant") is None
