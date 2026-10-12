"""Tests for django_rls_tenants.tenants.middleware."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from functools import partial
from unittest.mock import MagicMock, call, patch

import pytest
from asgiref.sync import async_to_sync, iscoroutinefunction, sync_to_async
from django.db import connection
from django.test import override_settings

from django_rls_tenants.exceptions import RLSConfigurationError
from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants.conf import RLSTenantsConfig
from django_rls_tenants.tenants.middleware import (
    RLSTenantMiddleware,
    _clear_gucs_set_flag,
    _reset_or_clear,
    _were_gucs_set,
)
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    get_rls_context_active,
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

pytestmark = pytest.mark.django_db

# Patch target for the module-level singleton used by middleware.
_CONF_PATCH = "django_rls_tenants.tenants.conf.rls_tenants_config"


def _make_request(user=None):
    """Create a mock HttpRequest with optional user."""
    request = MagicMock()
    if user is not None:
        request.user = user
    else:
        del request.user  # hasattr(request, 'user') will be False
    return request


class TestProcessRequest:
    """Tests for RLSTenantMiddleware.process_request()."""

    def test_tenant_user_sets_gucs(self, tenant_a_user):
        """Authenticated tenant user sets correct GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") == "false"
        assert get_guc("rls.current_tenant") == str(tenant_a_user.rls_tenant_id)

    def test_admin_user_sets_gucs(self, admin_user):
        """Authenticated admin user sets admin GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=admin_user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") == "true"
        # Admin mode clears tenant GUC; admin_bypass clause handles access.
        assert get_guc("rls.current_tenant") is None

    def test_unauthenticated_no_gucs(self):
        """Unauthenticated request sets no GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = False
        request = _make_request(user=user)
        mw.process_request(request)
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_no_user_attribute_no_gucs(self):
        """Request without user attribute sets no GUCs."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=None)  # no .user attribute
        mw.process_request(request)
        assert get_guc("rls.is_admin") is None


class TestProcessResponse:
    """Tests for RLSTenantMiddleware.process_response()."""

    def test_clears_gucs(self, tenant_a_user):
        """process_response clears GUCs after request processing."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        # GUCs are now set
        assert get_guc("rls.current_tenant") is not None
        # process_response should clear them
        response = MagicMock()
        mw.process_response(request, response)
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    @pytest.mark.parametrize(("outer_tenant", "outer_active"), [(None, False), (999, True)])
    def test_response_after_exception_restores_context_once(
        self, tenant_a_user, outer_tenant, outer_active
    ):
        """An error response must not reuse tokens or erase the restored context."""
        tenant_token = set_current_tenant_id(outer_tenant)
        active_token = set_rls_context_active(outer_active)
        try:
            mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
            request = _make_request(user=tenant_a_user)
            mw.process_request(request)
            mw.process_exception(request, RuntimeError("view failed"))
            assert get_current_tenant_id() == outer_tenant
            assert get_rls_context_active() is outer_active

            response = MagicMock()
            assert mw.process_response(request, response) is response
            assert get_current_tenant_id() == outer_tenant
            assert get_rls_context_active() is outer_active
            assert get_guc("rls.is_admin") is None
            assert get_guc("rls.current_tenant") is None
        finally:
            reset_current_tenant_id(tenant_token)
            reset_rls_context_active(active_token)

    def test_returns_response(self, tenant_a_user):
        """process_response returns the response object."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        response = MagicMock()
        result = mw.process_response(request, response)
        assert result is response

    def test_skips_clear_when_use_local_set_true(self, tenant_a_user):
        """process_response skips clearing GUCs when USE_LOCAL_SET=True."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_guc("rls.current_tenant") is not None

        with patch("django_rls_tenants.tenants.middleware.rls_tenants_config") as mock_conf:
            mock_conf.USE_LOCAL_SET = True
            response = MagicMock()
            mw.process_response(request, response)

        # GUCs should still be set (SET LOCAL auto-clears at transaction end)
        assert get_guc("rls.current_tenant") is not None


class TestThreadLocalFlags:
    """Tests for thread-local GUC flag helpers."""

    def test_were_gucs_set_false_by_default(self):
        """_were_gucs_set() returns False when no flag has been set."""
        _clear_gucs_set_flag()
        assert _were_gucs_set() is False

    def test_mark_and_check(self, tenant_a_user):
        """_mark_gucs_set marks the flag, _were_gucs_set reads it."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        _clear_gucs_set_flag()
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert _were_gucs_set() is True


class TestAutoScopeState:
    """Tests for ContextVar tenant state management in middleware."""

    def test_sets_state_for_tenant_user(self, tenant_a_user):
        """Middleware sets ContextVar state for tenant user."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_current_tenant_id() == tenant_a_user.rls_tenant_id

    def test_clears_state_for_admin_user(self, admin_user):
        """Middleware sets ContextVar state to None for admin user."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=admin_user)
        mw.process_request(request)
        assert get_current_tenant_id() is None

    def test_no_state_for_unauthenticated(self):
        """Middleware does not set ContextVar state for unauthenticated request."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = False
        request = _make_request(user=user)
        mw.process_request(request)
        assert get_current_tenant_id() is None

    def test_process_response_clears_state(self, tenant_a_user):
        """process_response clears ContextVar state."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        assert get_current_tenant_id() is not None
        response = MagicMock()
        mw.process_response(request, response)
        assert get_current_tenant_id() is None

    def test_full_request_response_cycle(self, tenant_a_user):
        """Full request/response cycle sets and clears ContextVar state."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)

        # Before request
        assert get_current_tenant_id() is None

        # After process_request
        mw.process_request(request)
        assert get_current_tenant_id() == tenant_a_user.rls_tenant_id

        # After process_response
        mw.process_response(request, MagicMock())
        assert get_current_tenant_id() is None


class TestProcessRequestExceptionHandling:
    """Tests for error handling in process_request."""

    def test_clears_gucs_and_reraises_on_resolve_error(self):
        """When _resolve_user_guc_vars raises, both GUCs and state are cleared."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = True
        request = _make_request(user=user)

        # Pre-set a GUC to verify cleanup
        set_guc("rls.is_admin", "true")
        set_guc("rls.current_tenant", "99")

        with (
            patch(
                "django_rls_tenants.tenants.middleware._resolve_user_guc_vars",
                side_effect=RuntimeError("bad user"),
            ),
            pytest.raises(RuntimeError, match="bad user"),
        ):
            mw.process_request(request)

        # Both GUCs should have been cleared as safety measure
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None
        # ContextVar state should also be cleared
        assert get_current_tenant_id() is None

    def test_survives_double_failure_on_cleanup(self):
        """When both set_guc and cleanup clear_guc fail, the original error propagates."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = True
        request = _make_request(user=user)

        with (
            patch(
                "django_rls_tenants.tenants.middleware._resolve_user_guc_vars",
                side_effect=RuntimeError("connection lost"),
            ),
            patch(
                "django_rls_tenants.tenants.middleware.clear_guc",
                side_effect=RuntimeError("cleanup also failed"),
            ),
            pytest.raises(RuntimeError, match="connection lost"),
        ):
            mw.process_request(request)


class TestMultiDatabaseGUC:
    """Tests for multi-database GUC support in middleware."""

    def test_sets_gucs_on_multiple_databases(self, tenant_a_user):
        """Middleware sets GUCs on all configured database aliases."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)

        # Mock set_guc to track all calls with their db_alias
        calls = []
        original_set_guc = set_guc

        def tracking_set_guc(name, value, *, is_local=False, using="default"):
            calls.append({"name": name, "value": value, "using": using})
            return original_set_guc(name, value, is_local=is_local, using=using)

        with (
            patch(
                "django_rls_tenants.tenants.middleware.rls_tenants_config",
            ) as mock_conf,
            patch(
                "django_rls_tenants.tenants.middleware.set_guc",
                side_effect=tracking_set_guc,
            ),
        ):
            mock_conf.DATABASES = ["default"]
            mock_conf.USE_LOCAL_SET = False
            mock_conf.GUC_PREFIX = "rls"
            mock_conf.GUC_CURRENT_TENANT = "rls.current_tenant"
            mock_conf.GUC_IS_ADMIN = "rls.is_admin"
            mw.process_request(request)

        # Should have set GUCs on 'default'
        default_calls = [c for c in calls if c["using"] == "default"]
        assert len(default_calls) == 2

    def test_cleanup_clears_all_databases(self, tenant_a_user):
        """_cleanup_rls_state clears GUCs on all configured database aliases."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)

        # Verify GUCs are set
        assert get_guc("rls.current_tenant") is not None

        # Now clear via process_response
        response = MagicMock()
        mw.process_response(request, response)

        # GUCs should be cleared on default
        assert get_guc("rls.is_admin") is None
        assert get_guc("rls.current_tenant") is None

    def test_error_on_one_db_clears_completed_dbs(self):
        """If GUC setting fails on one DB, completed DBs are cleaned up."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        user = MagicMock()
        user.is_authenticated = True
        user.is_tenant_admin = False
        user.rls_tenant_id = 42
        request = _make_request(user=user)

        call_count = 0
        cleared_aliases: list[str] = []

        def failing_set_guc(name, value, *, is_local=False, using="default"):
            nonlocal call_count
            call_count += 1
            # Fail on 3rd call (first call to second DB alias)
            if call_count >= 3:
                raise RuntimeError("Connection to replica failed")

        def tracking_clear_guc(name, *, is_local=False, using="default"):
            cleared_aliases.append(using)

        mock_conf = MagicMock()
        mock_conf.DATABASES = ["default", "replica"]
        mock_conf.USE_LOCAL_SET = False
        mock_conf.GUC_PREFIX = "rls"
        mock_conf.GUC_CURRENT_TENANT = "rls.current_tenant"
        mock_conf.GUC_IS_ADMIN = "rls.is_admin"

        with (
            patch(
                "django_rls_tenants.tenants.middleware.rls_tenants_config",
                mock_conf,
            ),
            patch(
                "django_rls_tenants.tenants.middleware.set_guc",
                side_effect=failing_set_guc,
            ),
            patch(
                "django_rls_tenants.tenants.middleware.clear_guc",
                side_effect=tracking_clear_guc,
            ),
            patch(
                "django_rls_tenants.tenants.middleware._resolve_user_guc_vars",
                return_value={
                    "rls.is_admin": "false",
                    "rls.current_tenant": "42",
                },
            ),
            patch(
                "django_rls_tenants.tenants.middleware._clear_gucs_on_all_databases",
                side_effect=lambda conf: None,
            ),
            pytest.raises(RuntimeError, match="Connection to replica failed"),
        ):
            mw.process_request(request)

        # "default" was completed, so it should be cleaned up
        assert "default" in cleared_aliases

    def test_cleanup_iterates_configured_databases(self, tenant_a_user):
        """_cleanup_rls_state iterates over conf.DATABASES, not just default."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)

        clear_calls = []
        original_clear_guc = __import__(
            "django_rls_tenants.rls.guc", fromlist=["clear_guc"]
        ).clear_guc

        def tracking_clear_guc(name, *, is_local=False, using="default"):
            clear_calls.append({"name": name, "using": using})
            return original_clear_guc(name, is_local=is_local, using=using)

        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default"],
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.middleware.clear_guc",
                side_effect=tracking_clear_guc,
            ),
        ):
            response = MagicMock()
            mw.process_response(request, response)

        # Should have cleared GUCs on 'default'
        default_clears = [c for c in clear_calls if c["using"] == "default"]
        assert len(default_clears) == 2  # is_admin + current_tenant


# ---- USE_LOCAL_SET=True: middleware-owned transaction (#95) ----

_LOCAL_SET = {"TENANT_MODEL": "test_app.Tenant", "USE_LOCAL_SET": True}


@pytest.mark.django_db(transaction=True)
class TestLocalSetTransaction:
    """``__call__`` opens ``transaction.atomic()`` per alias when ``USE_LOCAL_SET=True``.

    Django's ``ATOMIC_REQUESTS`` wraps only the view, never ``process_request``,
    so ``SET LOCAL`` issued there used to raise ``RuntimeError`` on every
    authenticated request (#95). ``transaction=True`` so there is no outer test
    transaction masking ``in_atomic_block``.
    """

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_authenticated_request_runs_inside_transaction(self, tenant_a_user):
        """SET LOCAL succeeds, is visible to the view, and is gone afterwards."""
        seen = {}

        def view(request):
            seen["in_atomic_block"] = connection.in_atomic_block
            seen["tenant"] = get_guc("rls.current_tenant")
            seen["is_admin"] = get_guc("rls.is_admin")
            return MagicMock()

        mw = RLSTenantMiddleware(get_response=view)
        assert connection.in_atomic_block is False
        mw(_make_request(user=tenant_a_user))

        assert seen == {
            "in_atomic_block": True,
            "tenant": str(tenant_a_user.tenant_id),
            "is_admin": "false",
        }
        # The transaction committed: SET LOCAL values are gone, no block left open.
        assert connection.in_atomic_block is False
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None
        # ContextVars reset by process_response.
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_unauthenticated_request_opens_no_transaction(self):
        """Health checks and anonymous requests pay for no transaction."""
        seen = {}

        def view(request):
            seen["in_atomic_block"] = connection.in_atomic_block
            return MagicMock()

        RLSTenantMiddleware(get_response=view)(_make_request())
        assert seen == {"in_atomic_block": False}

    def test_session_mode_opens_no_transaction(self, tenant_a_user):
        """``USE_LOCAL_SET=False`` (default) is unchanged: autocommit around the view."""
        seen = {}

        def view(request):
            seen["in_atomic_block"] = connection.in_atomic_block
            seen["tenant"] = get_guc("rls.current_tenant")
            return MagicMock()

        RLSTenantMiddleware(get_response=view)(_make_request(user=tenant_a_user))
        assert seen == {"in_atomic_block": False, "tenant": str(tenant_a_user.tenant_id)}
        assert get_guc("rls.current_tenant") is None  # cleared by process_response

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_exception_from_chain_rolls_back_and_propagates(self, tenant_a_user):
        """An exception escaping the chain rolls the transaction back; nothing leaks."""

        def view(request):
            assert get_guc("rls.current_tenant") == str(tenant_a_user.tenant_id)
            raise RuntimeError("boom")

        mw = RLSTenantMiddleware(get_response=view)
        request = _make_request(user=tenant_a_user)
        with pytest.raises(RuntimeError, match="boom"):
            mw(request)
        assert connection.in_atomic_block is False
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None
        # Even a directly escaping exception must clean up without relying on
        # process_exception or request_finished.
        assert request._rls_owns_transaction is False
        assert _were_gucs_set() is False
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False

    @override_settings(RLS_TENANTS={**_LOCAL_SET, "DATABASES": ["default", "replica"]})
    def test_opens_one_transaction_per_configured_alias(self, tenant_a_user):
        """Every alias in RLS_TENANTS['DATABASES'] gets its own atomic() block."""
        with (
            patch("django_rls_tenants.tenants.middleware.transaction.atomic") as atomic,
            patch.object(RLSTenantMiddleware, "process_request", return_value=None),
            patch.object(
                RLSTenantMiddleware, "process_response", side_effect=lambda r, resp: resp
            ),
        ):
            RLSTenantMiddleware(get_response=lambda r: MagicMock())(
                _make_request(user=tenant_a_user),
            )
        assert atomic.call_args_list == [call(using="default"), call(using="replica")]
        assert atomic.return_value.__enter__.call_count == 2
        assert atomic.return_value.__exit__.call_count == 2


class TestLocalSetUnderAsgi:
    """The async path refuses ``USE_LOCAL_SET=True`` with a clear configuration error."""

    @staticmethod
    def _async_middleware():
        async def aget_response(request):
            return MagicMock(name="response")

        return RLSTenantMiddleware(get_response=aget_response)

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_authenticated_request_raises_configuration_error(self, tenant_a_user):
        mw = self._async_middleware()
        assert iscoroutinefunction(mw) is True
        with pytest.raises(RLSConfigurationError, match="under ASGI"):
            async_to_sync(mw)(_make_request(user=tenant_a_user))

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_unauthenticated_request_passes_through(self):
        response = async_to_sync(self._async_middleware())(_make_request())
        assert response._mock_name == "response"


def _hop(func, *args):
    """Run ``func`` like one ``sync_to_async`` hop of ``MiddlewareMixin.__acall__``.

    The call runs in a copy of the current context; afterwards every changed
    value is copied back with a fresh ``set()``, exactly like asgiref's
    ``_restore_context()``. Tokens created inside the hop therefore belong to a
    context that no longer exists.
    """
    ctx = contextvars.copy_context()
    try:
        return ctx.run(func, *args)
    finally:
        for var, value in ctx.items():
            try:
                changed = var.get() is not value
            except LookupError:
                changed = True
            if changed:
                var.set(value)


class TestTokenResetAcrossContexts:
    """#58: tokens created in one sync_to_async hop are reset in another under ASGI."""

    @staticmethod
    def _run_async_middleware(user):
        seen = {}

        async def aget_response(request):
            seen["tenant_id"] = get_current_tenant_id()
            seen["active"] = get_rls_context_active()
            seen["tenant_guc"] = await sync_to_async(get_guc, thread_sensitive=True)(
                "rls.current_tenant"
            )
            seen["is_admin_guc"] = await sync_to_async(get_guc, thread_sensitive=True)(
                "rls.is_admin"
            )
            return MagicMock(name="response")

        mw = RLSTenantMiddleware(get_response=aget_response)
        assert iscoroutinefunction(mw) is True
        response = async_to_sync(mw)(_make_request(user=user))
        return response, seen

    @staticmethod
    def _assert_nothing_left_behind():
        assert get_current_tenant_id() is None
        assert get_rls_context_active() is False
        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None
        assert _were_gucs_set() is False

    def test_async_path_tenant_request_sets_state_and_cleans_up(self, tenant_a_user):
        response, seen = self._run_async_middleware(tenant_a_user)

        # The state set by process_request crosses the hop into the async view.
        assert seen == {
            "tenant_id": tenant_a_user.rls_tenant_id,
            "active": True,
            "tenant_guc": str(tenant_a_user.rls_tenant_id),
            "is_admin_guc": "false",
        }
        assert response._mock_name == "response"
        self._assert_nothing_left_behind()

    def test_async_path_admin_request_sets_state_and_cleans_up(self, admin_user):
        response, seen = self._run_async_middleware(admin_user)

        assert seen["tenant_id"] is None
        assert seen["active"] is True
        assert seen["is_admin_guc"] == "true"
        assert response._mock_name == "response"
        self._assert_nothing_left_behind()

    @pytest.mark.parametrize("user_fixture", ["tenant_a_user", "admin_user"])
    def test_cancelled_request_cleans_up(self, user_fixture, request):
        """Cancellation bypasses Django's response and exception hooks."""
        user = request.getfixturevalue(user_fixture)
        seen = {}

        async def run():
            started = asyncio.Event()

            async def aget_response(request):
                assert get_rls_context_active() is True
                started.set()
                await asyncio.Event().wait()

            mw = RLSTenantMiddleware(get_response=aget_response)

            async def serve_request():
                try:
                    await mw(_make_request(user=user))
                finally:
                    # Inspect the cancelled task's own ContextVars and the same
                    # thread-sensitive connection used by process_request.
                    seen["tenant_id"] = get_current_tenant_id()
                    seen["active"] = get_rls_context_active()
                    seen["gucs_set"] = _were_gucs_set()
                    seen["tenant_guc"] = await sync_to_async(get_guc)("rls.current_tenant")
                    seen["admin_guc"] = await sync_to_async(get_guc)("rls.is_admin")

            task = asyncio.create_task(serve_request())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        async_to_sync(run)()

        assert seen == {
            "tenant_id": None,
            "active": False,
            "gucs_set": False,
            "tenant_guc": None,
            "admin_guc": None,
        }

    def test_process_exception_in_another_context_falls_back(self, tenant_a_user):
        """process_exception, then the second pass in process_response, both succeed."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)

        _hop(mw.process_request, request)
        assert get_current_tenant_id() == tenant_a_user.rls_tenant_id
        _hop(mw.process_exception, request, RuntimeError("boom"))
        _hop(mw.process_response, request, MagicMock())

        assert request._rls_state_reset is True
        self._assert_nothing_left_behind()

    def test_fallback_logs_at_debug(self, tenant_a_user, caplog):
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        _hop(mw.process_request, request)

        with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
            _hop(mw.process_response, request, MagicMock())

        assert any("clearing rls_current_tenant_id" in msg for msg in caplog.messages)
        assert any("clearing rls_context_active" in msg for msg in caplog.messages)

    def test_runtime_error_from_double_reset_is_not_swallowed(self):
        token = set_current_tenant_id(5)
        clear = partial(set_current_tenant_id, None)
        _reset_or_clear(token, reset_current_tenant_id, clear)
        with pytest.raises(RuntimeError, match="already been used"):
            _reset_or_clear(token, reset_current_tenant_id, clear)

    def test_same_context_reset_uses_token(self):
        """On WSGI the token is valid: the previous value is restored, not cleared."""
        outer = set_current_tenant_id(7)
        try:
            token = set_current_tenant_id(8)
            _reset_or_clear(token, reset_current_tenant_id, partial(set_current_tenant_id, None))
            assert get_current_tenant_id() == 7
        finally:
            reset_current_tenant_id(outer)

    def test_missing_token_clears(self):
        set_rls_context_active(True)
        _reset_or_clear(None, reset_rls_context_active, partial(set_rls_context_active, False))
        assert get_rls_context_active() is False

    def test_guc_clearing_survives_a_failing_reset(self, tenant_a_user):
        """A reset error must not skip the GUC clear (it still propagates)."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)
        reset_current_tenant_id(request._rls_tenant_token)  # token now spent

        with pytest.raises(RuntimeError, match="already been used"):
            mw.process_response(request, MagicMock())

        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None
        assert _were_gucs_set() is False


class TestProcessExceptionRollback:
    """Rollback is deferred until the middleware-owned transactions exit."""

    @override_settings(RLS_TENANTS={**_LOCAL_SET, "DATABASES": ["default", "replica"]})
    def test_marks_every_alias_only_after_the_chain_returns(self, tenant_a_user):
        request = _make_request(user=tenant_a_user)
        conn = MagicMock(in_atomic_block=True)
        with (
            patch("django_rls_tenants.tenants.middleware.connections") as connections,
            patch("django_rls_tenants.tenants.middleware.transaction.atomic"),
            patch("django_rls_tenants.tenants.middleware.transaction.set_rollback") as set_rb,
            patch.object(RLSTenantMiddleware, "process_request", return_value=None),
            patch.object(RLSTenantMiddleware, "_cleanup_rls_state"),
        ):
            connections.__getitem__.return_value = conn

            def view(request):
                mw.process_exception(request, RuntimeError("boom"))
                set_rb.assert_not_called()
                return MagicMock()

            mw = RLSTenantMiddleware(get_response=view)
            mw(request)

        assert set_rb.call_args_list == [call(True, using="default"), call(True, using="replica")]
        assert request._rls_owns_transaction is False

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_no_rollback_when_middleware_did_not_open_the_transaction(self):
        request = _make_request()
        del request._rls_owns_transaction
        with patch("django_rls_tenants.tenants.middleware.transaction.set_rollback") as set_rb:
            RLSTenantMiddleware(get_response=lambda r: MagicMock()).process_exception(
                request, RuntimeError("boom")
            )
        set_rb.assert_not_called()

    @override_settings(
        MIDDLEWARE=[
            "django_rls_tenants.RLSTenantMiddleware",
            "tests.rls_views.DatabaseExceptionMiddleware",
        ],
    )
    def test_session_mode_allows_downstream_exception_handlers(self):
        RLSTenantMiddleware(get_response=lambda r: MagicMock())
