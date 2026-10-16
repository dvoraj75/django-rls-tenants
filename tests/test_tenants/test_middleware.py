"""Tests for django_rls_tenants.tenants.middleware."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from unittest.mock import MagicMock, call, patch

import pytest
from asgiref.sync import async_to_sync, iscoroutinefunction, sync_to_async
from django.core.signals import request_finished
from django.db import OperationalError, close_old_connections, connection
from django.http import HttpRequest
from django.test import override_settings
from django.utils.functional import SimpleLazyObject

from django_rls_tenants.exceptions import NoTenantContextError, RLSConfigurationError
from django_rls_tenants.rls.guc import get_guc, set_guc
from django_rls_tenants.tenants import middleware as middleware_module
from django_rls_tenants.tenants.conf import RLSTenantsConfig
from django_rls_tenants.tenants.context import tenant_context
from django_rls_tenants.tenants.middleware import (
    RLSTenantMiddleware,
    _clear_gucs_set_flag,
    _end_context_gucs,
    _mark_gucs_set,
    _reset_or_clear,
    _were_gucs_set,
    _were_gucs_set_on_this_thread,
)
from django_rls_tenants.tenants.state import (
    ContextGucs,
    _get_context_gucs,
    _reset_context_gucs,
    _set_context_gucs,
    get_current_tenant_id,
    get_rls_context_active,
    reset_current_tenant_id,
    reset_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)
from tests.session_gucs import session_gucs

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

        # GUCs should still be set (SET LOCAL auto-clears at transaction end). Read
        # with a driver cursor: the patch reaches the middleware only, so the
        # affinity guard, still in session mode, would clear them first.
        assert session_gucs()[0] is not None


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

    def test_flag_belongs_to_the_thread_that_set_it(self):
        """A thread with a copy of the context sees the flag but does not own the GUCs (#130)."""
        _mark_gucs_set()

        def other_thread():
            return _were_gucs_set(), _were_gucs_set_on_this_thread()

        with ThreadPoolExecutor(max_workers=1) as executor:
            other = executor.submit(contextvars.copy_context().run, other_thread).result()

        assert (_were_gucs_set(), _were_gucs_set_on_this_thread()) == (True, True)
        assert other == (True, False)

    def test_unset_flag_is_owned_by_no_thread(self):
        """Also on a thread that has no marker yet."""
        _clear_gucs_set_flag()

        with ThreadPoolExecutor(max_workers=1) as executor:
            owned = executor.submit(
                contextvars.copy_context().run, _were_gucs_set_on_this_thread
            ).result()

        assert owned is False
        assert _were_gucs_set_on_this_thread() is False

    def test_finished_threads_marker_matches_no_later_thread(self):
        """A later thread may reuse a finished thread's ident (glibc does), never its marker."""

        def owner():
            _mark_gucs_set()
            return _were_gucs_set_on_this_thread()

        context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(context.run, owner).result() is True
        # Leaving the block joined that thread; this executor starts a new one.
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(context.run, _were_gucs_set_on_this_thread).result() is False


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
        """When both the set and the cleanup's clear fail, the original error propagates."""
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
                "django_rls_tenants.tenants.middleware.write_gucs",
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

        # Track every write with the alias of its connection
        calls = []
        original_write_gucs = middleware_module.write_gucs

        def tracking_write_gucs(conn, values, *, is_local=False):
            calls.append((conn.alias, values))
            return original_write_gucs(conn, values, is_local=is_local)

        with (
            patch(
                "django_rls_tenants.tenants.middleware.rls_tenants_config",
            ) as mock_conf,
            patch(
                "django_rls_tenants.tenants.middleware.write_gucs",
                side_effect=tracking_write_gucs,
            ),
        ):
            mock_conf.DATABASES = ["default"]
            mock_conf.USE_LOCAL_SET = False
            mock_conf.GUC_PREFIX = "rls"
            mock_conf.GUC_CURRENT_TENANT = "rls.current_tenant"
            mock_conf.GUC_IS_ADMIN = "rls.is_admin"
            mw.process_request(request)

        # Both GUCs set on 'default' in one statement
        assert calls == [("default", ContextGucs.for_tenant(tenant_a_user.rls_tenant_id))]

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

        written: list[tuple[str, ContextGucs | None]] = []

        def failing_write_gucs(conn, values, *, is_local=False):
            written.append((conn.alias, values))
            if conn.alias == "replica" and values is not None:
                raise RuntimeError("Connection to replica failed")

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
                "django_rls_tenants.tenants.middleware.connections",
                {alias: MagicMock(alias=alias) for alias in mock_conf.DATABASES},
            ),
            patch(
                "django_rls_tenants.tenants.middleware.write_gucs",
                side_effect=failing_write_gucs,
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

        # "default" was completed, so it is cleaned up; "replica" is not
        gucs = ContextGucs("false", "42")
        assert written == [("default", gucs), ("replica", gucs), ("default", None)]

    def test_cleanup_iterates_configured_databases(self, tenant_a_user):
        """_cleanup_rls_state iterates over conf.DATABASES, not just default."""
        mw = RLSTenantMiddleware(get_response=lambda r: MagicMock())
        request = _make_request(user=tenant_a_user)
        mw.process_request(request)

        clear_calls = []
        original_write_gucs = middleware_module.write_gucs

        def tracking_write_gucs(conn, values, *, is_local=False):
            clear_calls.append((conn.alias, values))
            return original_write_gucs(conn, values, is_local=is_local)

        with (
            override_settings(
                RLS_TENANTS={
                    "TENANT_MODEL": "test_app.Tenant",
                    "DATABASES": ["default"],
                },
            ),
            patch(_CONF_PATCH, RLSTenantsConfig()),
            patch(
                "django_rls_tenants.tenants.middleware.write_gucs",
                side_effect=tracking_write_gucs,
            ),
        ):
            response = MagicMock()
            mw.process_response(request, response)

        # Both GUCs cleared on 'default' in one statement
        assert clear_calls == [("default", None)]


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


@pytest.mark.django_db(transaction=True)
class TestLocalSetUnderAsgi:
    """The async path serves ``USE_LOCAL_SET=True`` with no transaction of its own (#96).

    ``transaction=True``: an ASGI request runs in autocommit, outside any test transaction.
    """

    @staticmethod
    def _async_middleware(aget_response=None):
        async def respond(request):
            return MagicMock(name="response")

        return RLSTenantMiddleware(get_response=aget_response or respond)

    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_authenticated_request_runs_each_statement_in_its_own_transaction(
        self,
        tenant_a_user,
    ):
        seen = {}

        def probe():
            return (
                connection.in_atomic_block,
                get_guc("rls.current_tenant"),
                session_gucs(),
            )

        async def aget_response(request):
            seen["probe"] = await sync_to_async(probe)()
            seen["tenant"] = get_current_tenant_id()
            return MagicMock(name="response")

        mw = self._async_middleware(aget_response)
        assert iscoroutinefunction(mw) is True
        response = async_to_sync(mw)(_make_request(user=tenant_a_user))

        assert response._mock_name == "response"
        tenant_id = tenant_a_user.rls_tenant_id
        assert seen == {"probe": (False, str(tenant_id), (None, None)), "tenant": tenant_id}
        assert session_gucs() == (None, None)

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
    """#58: tokens created in one sync_to_async hop are reset in another under ASGI.

    Since #38 the ``_run_async_middleware`` and cancellation tests run the native
    ``__acall__``. The ``_hop`` tests cover the fallback that the hook-based path,
    still used by subclasses that override the sync hooks, depends on.
    """

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


class TestContextGucsCleanup:
    """The guard's context values end with the request, however the request ends."""

    def test_end_context_gucs_clears_even_when_the_reset_raises(self):
        token = _set_context_gucs(ContextGucs.for_tenant(1))
        _reset_context_gucs(token)  # the token is now used up: resetting raises
        _set_context_gucs(ContextGucs.for_tenant(2))

        with pytest.raises(RuntimeError):
            _end_context_gucs(token)

        assert _get_context_gucs() is None

    def test_base_exception_from_the_view_still_ends_the_context(self, tenant_a_user):
        """Not an Exception, so Django does not turn it into a response (WSGI)."""

        class _Abort(BaseException):
            pass

        def abort(request):
            raise _Abort

        with pytest.raises(_Abort):
            RLSTenantMiddleware(get_response=abort)(_make_request(user=tenant_a_user))

        assert _get_context_gucs() is None
        assert get_current_tenant_id() is None
        assert get_guc("rls.current_tenant") is None
        assert _were_gucs_set() is False


class TestCleanupOfTheTenantValues:
    """What the cleanup does to the tenant ``ContextVar`` and the strict-mode flag."""

    @pytest.mark.parametrize("inside_a_block", [False, True])
    def test_base_exception_in_process_request_resets_what_it_set(
        self,
        inside_a_block,
        tenant_a,
        tenant_b_user,
        monkeypatch,
    ):
        """A signal-raised exception after the tenant was set and before the rest."""

        class _Abort(BaseException):
            pass

        def abort(active):
            raise _Abort

        middleware = RLSTenantMiddleware(get_response=lambda request: None)
        block = tenant_context(tenant_a.pk) if inside_a_block else contextlib.nullcontext()

        with block:
            monkeypatch.setattr(middleware_module, "set_rls_context_active", abort)
            with pytest.raises(_Abort):
                middleware(_make_request(user=tenant_b_user))
            monkeypatch.undo()
            tenant_id = get_current_tenant_id()

        assert tenant_id == (tenant_a.pk if inside_a_block else None)

    def test_anonymous_request_clears_what_its_view_left(self):
        def view(request):
            set_current_tenant_id(7)
            set_rls_context_active(True)

        RLSTenantMiddleware(get_response=view)(_make_request())

        assert (get_current_tenant_id(), get_rls_context_active()) == (None, False)


class TestMiddlewareContextValues:
    """The context values the GUC affinity guard applies, as the middleware sets and ends them."""

    def test_request_inside_a_context_gives_the_outer_context_back(self, tenant_a, tenant_b_user):
        middleware = RLSTenantMiddleware(get_response=lambda request: None)
        request = _make_request(user=tenant_b_user)

        with tenant_context(tenant_a.pk):
            middleware.process_request(request)
            middleware.process_response(request, MagicMock())

            assert _get_context_gucs() == ContextGucs.for_tenant(tenant_a.pk)
            assert get_guc("rls.current_tenant") == str(tenant_a.pk)

    def test_failed_request_setup_leaves_no_context_values(self, tenant_a_user, monkeypatch):
        def fail():
            msg = "setup failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(middleware_module, "_mark_gucs_set", fail)
        middleware = RLSTenantMiddleware(get_response=lambda request: None)

        with pytest.raises(RuntimeError, match="setup failed"):
            middleware.process_request(_make_request(user=tenant_a_user))

        assert _get_context_gucs() is None

    def test_cleanup_error_does_not_replace_a_base_exception(self, tenant_a_user, monkeypatch):
        class _Abort(BaseException):
            pass

        def abort(request):
            raise _Abort

        def failing_cleanup(request):
            msg = "cleanup failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            RLSTenantMiddleware,
            "_cleanup_rls_state",
            staticmethod(failing_cleanup),
        )

        with pytest.raises(_Abort):
            RLSTenantMiddleware(get_response=abort)(_make_request(user=tenant_a_user))


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

    @override_settings(
        RLS_TENANTS=_LOCAL_SET,
        MIDDLEWARE=[
            "django_rls_tenants.RLSTenantMiddleware",
            "tests.rls_views.DatabaseExceptionMiddleware",
        ],
    )
    def test_local_set_native_async_path_allows_downstream_exception_handlers(self):
        """The async path opens no request transaction, so there is no rollback to skip (#96)."""

        async def aget_response(request):
            return MagicMock()

        _async_mw(aget_response)
        with pytest.raises(RLSConfigurationError, match="before RLSTenantMiddleware"):
            RLSTenantMiddleware(get_response=lambda r: MagicMock())


# ---- Native async path (#38) ----


def _async_mw(aget_response):
    """Build the middleware the way Django does for an async downstream chain."""
    mw = RLSTenantMiddleware(get_response=aget_response)
    assert iscoroutinefunction(mw) is True
    return mw


def _record_hops(monkeypatch):
    """Record the name of every function the middleware runs through ``sync_to_async``."""
    hops = []
    real = middleware_module.sync_to_async

    def recording(func, *args, **kwargs):
        hops.append(func.__name__)
        return real(func, *args, **kwargs)

    monkeypatch.setattr(middleware_module, "sync_to_async", recording)
    return hops


def _state():
    return get_current_tenant_id(), get_rls_context_active(), _were_gucs_set()


_AUTH_SIDE_EFFECT = contextvars.ContextVar("auth_side_effect", default=None)


def _assert_async_request_left_nothing_behind():
    assert _state() == (None, False, False)
    assert get_guc("rls.current_tenant") is None
    assert get_guc("rls.is_admin") is None


class TestNativeAsyncPath:
    """``__acall__`` serves ASGI requests itself instead of ``MiddlewareMixin.__acall__``."""

    def test_sync_hooks_are_not_used(self, tenant_a_user):
        async def aget_response(request):
            return MagicMock(name="response")

        with (
            patch.object(RLSTenantMiddleware, "process_request", side_effect=AssertionError),
            patch.object(RLSTenantMiddleware, "process_response", side_effect=AssertionError),
        ):
            response = async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert response._mock_name == "response"
        _assert_async_request_left_nothing_behind()

    def test_state_is_visible_in_downstream_hops(self, tenant_a_user):
        """Sync code run by the chain (sync views, the ORM) sees the request's state."""
        seen = {}

        async def aget_response(request):
            seen["hop"] = await sync_to_async(_state, thread_sensitive=True)()
            return MagicMock()

        async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert seen["hop"] == (tenant_a_user.rls_tenant_id, True, True)
        _assert_async_request_left_nothing_behind()

    def test_one_hop_on_entry_and_one_on_exit(self, tenant_a_user, monkeypatch):
        hops = _record_hops(monkeypatch)

        async def aget_response(request):
            assert hops == ["_enter_request_gucs"]
            return MagicMock()

        async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert hops == ["_enter_request_gucs", "_clear_request_gucs"]

    @pytest.mark.parametrize(
        "user", [MagicMock(is_authenticated=False), None], ids=["anonymous", "no-user"]
    )
    def test_unauthenticated_request_makes_one_hop_and_sets_nothing(self, user, monkeypatch):
        hops = _record_hops(monkeypatch)
        seen = {}

        async def aget_response(request):
            seen["state"] = _state()
            return MagicMock()

        async_to_sync(_async_mw(aget_response))(_make_request(user=user))

        assert seen["state"] == (None, False, False)
        assert hops == ["_enter_request_gucs"]
        _assert_async_request_left_nothing_behind()

    def test_contextvars_set_while_resolving_the_user_reach_the_view(self, tenant_a_user):
        """An auth backend's ContextVar writes survive the enter hop, as with process_request."""

        def load_user():
            _AUTH_SIDE_EFFECT.set("set by the auth backend")
            return tenant_a_user

        # A real HttpRequest: MagicMock.__setattr__ would evaluate the lazy object.
        request = HttpRequest()
        request.user = SimpleLazyObject(load_user)
        seen = {}

        async def aget_response(request):
            seen["side_effect"] = _AUTH_SIDE_EFFECT.get()
            seen["tenant_id"] = get_current_tenant_id()
            return MagicMock()

        async_to_sync(_async_mw(aget_response))(request)

        assert seen == {
            "side_effect": "set by the auth backend",
            "tenant_id": tenant_a_user.rls_tenant_id,
        }
        _assert_async_request_left_nothing_behind()

    @pytest.mark.parametrize(("outer_tenant", "outer_active"), [(None, False), (999, True)])
    def test_tokens_restore_the_outer_context(
        self, tenant_a_user, outer_tenant, outer_active, caplog
    ):
        """The tokens are reset in their own context: no #58 fallback, outer values return."""
        tenant_token = set_current_tenant_id(outer_tenant)
        active_token = set_rls_context_active(outer_active)
        try:

            async def aget_response(request):
                assert get_current_tenant_id() == tenant_a_user.rls_tenant_id
                return MagicMock()

            with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
                async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

            assert get_current_tenant_id() == outer_tenant
            assert get_rls_context_active() is outer_active
            assert not any("belongs to another Context" in m for m in caplog.messages)
        finally:
            reset_current_tenant_id(tenant_token)
            reset_rls_context_active(active_token)

    def test_only_the_enter_hop_thread_owns_the_gucs(self, tenant_a_user):
        """Not the event loop's thread, nor a ``thread_sensitive=False`` worker (#130)."""
        seen = {}

        async def aget_response(request):
            seen["loop"] = _were_gucs_set_on_this_thread()
            seen["hop"] = await sync_to_async(
                _were_gucs_set_on_this_thread, thread_sensitive=True
            )()
            seen["worker"] = await sync_to_async(
                _were_gucs_set_on_this_thread, thread_sensitive=False
            )()
            return MagicMock()

        async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert seen == {"loop": False, "hop": True, "worker": False}
        _assert_async_request_left_nothing_behind()

    def test_logs_set_and_clear_at_debug(self, tenant_a_user, caplog):
        async def aget_response(request):
            return MagicMock()

        with caplog.at_level(logging.DEBUG, logger="django_rls_tenants"):
            async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert any("Middleware set GUCs" in m for m in caplog.messages)
        assert any("Middleware cleared GUCs" in m for m in caplog.messages)

    def test_process_exception_ends_context_before_error_handlers(
        self, tenant_a_user, monkeypatch
    ):
        """Django runs process_exception in its own hop; error handlers then see no context."""
        hops = _record_hops(monkeypatch)
        seen = {}

        async def aget_response(request):
            # What BaseHandler._get_response_async does when the view raises.
            await sync_to_async(mw.process_exception, thread_sensitive=True)(
                request, RuntimeError("boom")
            )
            seen["state"] = _state()
            seen["guc"] = await sync_to_async(get_guc, thread_sensitive=True)("rls.current_tenant")
            return MagicMock(status_code=500)

        mw = _async_mw(aget_response)
        async_to_sync(mw)(_make_request(user=tenant_a_user))

        assert seen == {"state": (None, False, False), "guc": None}
        # process_exception already cleared the GUCs, so no exit hop is needed.
        assert hops == ["_enter_request_gucs"]
        _assert_async_request_left_nothing_behind()

    def test_exception_from_chain_cleans_up_and_propagates(self, tenant_a_user):
        """E.g. DEBUG_PROPAGATE_EXCEPTIONS: the chain raises instead of returning a 500."""

        async def aget_response(request):
            msg = "chain failed"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="chain failed"):
            async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))
        _assert_async_request_left_nothing_behind()

    @pytest.mark.django_db(transaction=True)
    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_use_local_set_writes_no_gucs(self, tenant_a_user, monkeypatch):
        """No transaction can span the awaited chain; the guard applies the values (#96)."""
        hops = _record_hops(monkeypatch)
        seen = {}

        async def aget_response(request):
            seen["state"] = _state()
            seen["values"] = _get_context_gucs()
            return MagicMock(name="response")

        with patch.object(middleware_module, "write_gucs", side_effect=AssertionError):
            async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        tenant_id = tenant_a_user.rls_tenant_id
        assert seen == {
            "state": (tenant_id, True, True),
            "values": ContextGucs.for_tenant(tenant_id),
        }
        assert hops == ["_enter_request_gucs", "_clear_request_gucs"]
        _assert_async_request_left_nothing_behind()

    def test_enter_failure_skips_chain_and_leaves_nothing(self):
        user = MagicMock(is_authenticated=True, is_tenant_admin=False, rls_tenant_id=None)

        async def aget_response(request):
            pytest.fail("The chain must not run after a failed entry")

        with pytest.raises(NoTenantContextError):
            async_to_sync(_async_mw(aget_response))(_make_request(user=user))
        _assert_async_request_left_nothing_behind()

    def test_guc_failure_in_enter_hop_clears_best_effort(self, tenant_a_user):
        async def aget_response(request):
            pytest.fail("The chain must not run after a failed entry")

        real_write_gucs = middleware_module.write_gucs

        def failing_set(conn, values, *, is_local=False):
            if values is not None:
                raise OperationalError("db down")
            return real_write_gucs(conn, values, is_local=is_local)

        with (
            patch.object(middleware_module, "write_gucs", side_effect=failing_set),
            patch.object(
                middleware_module,
                "_clear_gucs_on_all_databases",
                wraps=middleware_module._clear_gucs_on_all_databases,
            ) as best_effort,
            pytest.raises(OperationalError, match="db down"),
        ):
            async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))
        best_effort.assert_called_once()
        _assert_async_request_left_nothing_behind()

    def test_cancel_during_enter_hop_undoes_it(self, tenant_a_user, monkeypatch):
        """asgiref finishes a running hop after a cancel; the GUCs it set must be cleared."""
        real_enter = RLSTenantMiddleware._enter_request_gucs
        holder = {}

        def slow_enter(self, request, conf):
            # Cancel the awaiting task from inside the hop, then keep the hop busy
            # long enough for the cancellation to arrive while it runs.
            holder["loop"].call_soon_threadsafe(holder["task"].cancel)
            time.sleep(0.05)
            return real_enter(self, request, conf)

        monkeypatch.setattr(RLSTenantMiddleware, "_enter_request_gucs", slow_enter)

        async def aget_response(request):
            pytest.fail("The chain must not run after a cancelled entry")

        async def run():
            holder["loop"] = asyncio.get_running_loop()
            holder["task"] = asyncio.create_task(
                _async_mw(aget_response)(_make_request(user=tenant_a_user))
            )
            with pytest.raises(asyncio.CancelledError):
                await holder["task"]
            return await sync_to_async(get_guc, thread_sensitive=True)("rls.current_tenant")

        assert async_to_sync(run)() is None
        _assert_async_request_left_nothing_behind()

    def test_cancel_during_failing_enter_hop_keeps_the_cancellation(
        self, tenant_a_user, monkeypatch
    ):
        """A hop that fails while the task is being cancelled must not replace the cancel."""
        holder = {}

        def failing_enter(self, request, conf):
            holder["loop"].call_soon_threadsafe(holder["task"].cancel)
            time.sleep(0.05)
            msg = "enter failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(RLSTenantMiddleware, "_enter_request_gucs", failing_enter)

        async def aget_response(request):
            pytest.fail("The chain must not run after a cancelled entry")

        async def run():
            holder["loop"] = asyncio.get_running_loop()
            holder["task"] = asyncio.create_task(
                _async_mw(aget_response)(_make_request(user=tenant_a_user))
            )
            with pytest.raises(asyncio.CancelledError):
                await holder["task"]

        async_to_sync(run)()
        _assert_async_request_left_nothing_behind()

    def test_guc_clear_survives_a_failing_reset(self, tenant_a_user, monkeypatch):
        """A token reset that raises must not skip the GUC clear (the error still propagates)."""

        def failing_reset(token):
            msg = "reset failed"
            raise ValueError(msg)

        monkeypatch.setattr(middleware_module, "reset_current_tenant_id", failing_reset)

        async def aget_response(request):
            return MagicMock()

        with pytest.raises(ValueError, match="reset failed"):
            async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert get_guc("rls.current_tenant") is None
        assert get_guc("rls.is_admin") is None
        assert _were_gucs_set() is False

    def test_failed_clear_keeps_flag_for_the_safety_net(self, tenant_a_user, monkeypatch):
        def failing_clear(conf):
            msg = "clear failed"
            raise OperationalError(msg)

        monkeypatch.setattr(middleware_module, "_clear_request_gucs", failing_clear)

        async def aget_response(request):
            return MagicMock()

        with pytest.raises(OperationalError, match="clear failed"):
            async_to_sync(_async_mw(aget_response))(_make_request(user=tenant_a_user))

        assert _state() == (None, False, True)
        # Still on the session. A driver cursor reads it: the affinity guard would
        # clear it before any statement outside the context.
        assert session_gucs()[0] == str(tenant_a_user.rls_tenant_id)

        # close_old_connections would close the test's connection.
        request_finished.disconnect(close_old_connections)
        try:
            request_finished.send(sender=self.__class__)
        finally:
            request_finished.connect(close_old_connections)
        _assert_async_request_left_nothing_behind()


class _HookOverridingMiddleware(RLSTenantMiddleware):
    """A project subclass that customises both synchronous hooks."""

    def __init__(self, get_response):
        super().__init__(get_response)
        self.calls = []

    def process_request(self, request):
        self.calls.append("process_request")
        return super().process_request(request)

    def process_response(self, request, response):
        self.calls.append("process_response")
        return super().process_response(request, response)


class TestSubclassWithSyncHooks:
    """A subclass that overrides a sync hook keeps the hook-based async path (#38)."""

    @pytest.mark.parametrize("hook", ["process_request", "process_response"])
    def test_overriding_either_hook_selects_the_hook_path(self, hook):
        def override(self, *args):
            return getattr(RLSTenantMiddleware, hook)(self, *args)

        subclass = type("Subclass", (RLSTenantMiddleware,), {hook: override})

        def get_response(request):
            return MagicMock()

        assert subclass(get_response)._sync_hooks_overridden is True
        assert RLSTenantMiddleware(get_response)._sync_hooks_overridden is False

    def test_overridden_hooks_run_under_asgi(self, tenant_a_user, monkeypatch):
        hops = _record_hops(monkeypatch)
        seen = {}

        async def aget_response(request):
            seen["state"] = _state()
            return MagicMock(name="response")

        mw = _HookOverridingMiddleware(get_response=aget_response)
        assert iscoroutinefunction(mw) is True
        response = async_to_sync(mw)(_make_request(user=tenant_a_user))

        assert response._mock_name == "response"
        assert mw.calls == ["process_request", "process_response"]
        assert seen["state"] == (tenant_a_user.rls_tenant_id, True, True)
        # process_response already cleaned up, so the finally block makes no hop.
        assert hops == []
        _assert_async_request_left_nothing_behind()

    def test_hook_hop_thread_owns_the_gucs(self, tenant_a_user):
        """``process_request`` runs in a thread-sensitive hop; that thread owns the GUCs (#130)."""
        seen = {}

        async def aget_response(request):
            seen["hop"] = await sync_to_async(
                _were_gucs_set_on_this_thread, thread_sensitive=True
            )()
            seen["worker"] = await sync_to_async(
                _were_gucs_set_on_this_thread, thread_sensitive=False
            )()
            return MagicMock()

        async_to_sync(_HookOverridingMiddleware(get_response=aget_response))(
            _make_request(user=tenant_a_user)
        )

        assert seen == {"hop": True, "worker": False}
        _assert_async_request_left_nothing_behind()

    def test_cancelled_request_is_cleaned_up(self, tenant_a_user):
        """Cancellation skips Django's response hook; the finally block cleans up."""
        seen = {}

        async def run():
            started = asyncio.Event()

            async def aget_response(request):
                started.set()
                await asyncio.Event().wait()

            mw = _HookOverridingMiddleware(get_response=aget_response)

            async def serve_request():
                try:
                    await mw(_make_request(user=tenant_a_user))
                finally:
                    seen["calls"] = list(mw.calls)
                    seen["state"] = _state()
                    seen["guc"] = await sync_to_async(get_guc)("rls.current_tenant")

            task = asyncio.create_task(serve_request())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        async_to_sync(run)()

        assert seen == {"calls": ["process_request"], "state": (None, False, False), "guc": None}

    def test_cancelled_while_process_request_writes_the_gucs_still_clears_them(
        self, tenant_a_user, monkeypatch
    ):
        """asgiref copies the hop's values at the cancellation, before the hop marks the GUCs set.

        The request then never learns of the GUCs the hop goes on to write, so
        the cleanup would find nothing to clear: a clear is queued behind the hop
        instead (the native path does the same in ``_aenter_request``).
        """
        real_set = RLSTenantMiddleware._set_gucs_on_all_databases
        seen = {}

        async def run():
            loop = asyncio.get_running_loop()
            box = {}

            def slow_set(guc_vars, conf):
                loop.call_soon_threadsafe(box["task"].cancel)
                time.sleep(0.2)  # the loop handles the cancellation meanwhile
                real_set(guc_vars, conf)

            monkeypatch.setattr(
                RLSTenantMiddleware, "_set_gucs_on_all_databases", staticmethod(slow_set)
            )

            async def aget_response(request):
                raise AssertionError("the view must not run")

            mw = _HookOverridingMiddleware(get_response=aget_response)
            box["task"] = asyncio.create_task(mw(_make_request(user=tenant_a_user)))
            with pytest.raises(asyncio.CancelledError):
                await box["task"]
            seen["session"] = await sync_to_async(session_gucs, thread_sensitive=True)()
            seen["state"] = _state()

        async_to_sync(run)()

        assert seen == {"session": (None, None), "state": (None, False, False)}
        _assert_async_request_left_nothing_behind()

    def test_request_inside_a_block_leaves_the_blocks_values_in_place(
        self, tenant_a_user, tenant_b
    ):
        """The hooks' hops hand cleared values back by value; the request puts the block's back."""

        async def aget_response(request):
            return MagicMock(name="response")

        mw = _HookOverridingMiddleware(get_response=aget_response)
        with tenant_context(tenant_b.pk):
            async_to_sync(mw)(_make_request(user=tenant_a_user))

            assert (get_current_tenant_id(), get_rls_context_active()) == (tenant_b.pk, True)
            # The guard puts the block's GUCs back before the next statement.
            assert get_guc("rls.current_tenant") == str(tenant_b.pk)
        _assert_async_request_left_nothing_behind()

    @pytest.mark.django_db(transaction=True)
    @override_settings(RLS_TENANTS=_LOCAL_SET)
    def test_use_local_set_serves_the_request_without_a_transaction(self, tenant_a_user):
        """``process_request`` writes no GUC; the guard applies the values (#96)."""
        seen = {}

        def probe():
            return (
                connection.in_atomic_block,
                get_guc("rls.current_tenant"),
                session_gucs(),
            )

        async def aget_response(request):
            seen["probe"] = await sync_to_async(probe)()
            return MagicMock(name="response")

        mw = _HookOverridingMiddleware(get_response=aget_response)
        async_to_sync(mw)(_make_request(user=tenant_a_user))

        assert mw.calls == ["process_request", "process_response"]
        assert seen["probe"] == (False, str(tenant_a_user.rls_tenant_id), (None, None))
        _assert_async_request_left_nothing_behind()
