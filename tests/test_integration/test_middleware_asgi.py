"""End-to-end tests for ``RLSTenantMiddleware`` under ASGI (#58, #38, #130).

Drives real requests through the full middleware stack with ``django.test.AsyncClient``.
Before the #58 fix every authenticated request failed with ``ValueError: <Token ...> was
created in a different Context``: ``MiddlewareMixin.__acall__`` runs ``process_request``
and ``process_response`` / ``process_exception`` in separate ``sync_to_async`` hops,
each in its own copy of the request's ``contextvars.Context``, so the tokens saved by
``process_request`` could not be reset afterwards. Since #38 the middleware serves
these requests on its own async path; ``TestNativeAsyncPath`` covers it with async views.

``AsyncClientHandler`` fires ``request_finished`` on a throwaway connection, so the
"nothing left behind" assertions below check the middleware's own cleanup, not the
``request_finished`` safety net. ``TestErrorPageOnWorkerThread`` covers the error pages
Django builds on a worker thread while the request context is active (#130, #41).
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings as django_settings
from django.core.handlers.asgi import ASGIHandler
from django.db import connections
from django.test import AsyncClient, Client, override_settings

from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.rls.guc import get_guc
from django_rls_tenants.tenants.middleware import RLSTenantMiddleware
from django_rls_tenants.tenants.state import get_current_tenant_id, get_rls_context_active
from tests import admin_urls
from tests.test_app.models import AdminUser

pytestmark = pytest.mark.django_db

_RLS = "django_rls_tenants.tenants.middleware.RLSTenantMiddleware"

_MIDDLEWARE_WITH_RLS = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    _RLS,
]


@pytest.fixture
def scoped_web_user(tenant_a):
    """A real ``AUTH_USER_MODEL`` user scoped to tenant A (not an RLS admin)."""
    return AdminUser.objects.create_user(
        username="web_a",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_a,
        rls_admin=False,
    )


@pytest.fixture
def admin_web_user(db):
    """A real ``AUTH_USER_MODEL`` user with cross-tenant RLS admin rights."""
    return AdminUser.objects.create_user(
        username="web_admin",
        password="pw",  # noqa: S106  -- test-only credential
        rls_admin=True,
    )


@pytest.fixture
def staff_web_user(tenant_a):
    """A Django superuser scoped to tenant A, like the reporter's admin user."""
    return AdminUser.objects.create_superuser(
        username="staff_a",
        password="pw",  # noqa: S106  -- test-only credential
        tenant=tenant_a,
        rls_admin=False,
    )


@pytest.fixture
def rls_middleware():
    """Enable the RLS middleware for the request phase.

    Listed *last* in each test's fixture arguments so the data fixtures run
    before the middleware stack is swapped in.
    """
    with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_RLS):
        yield


def _assert_request_left_nothing_behind():
    assert get_guc("rls.current_tenant") is None
    assert get_guc("rls.is_admin") is None
    assert get_current_tenant_id() is None
    assert get_rls_context_active() is False


@pytest.mark.integration
class TestMiddlewareAsgiRequestCycle:
    """The default (session GUC) configuration works end to end under ASGI."""

    def test_tenant_request_sees_its_rows(
        self, scoped_web_user, sample_orders, tenant_a, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] == str(tenant_a.pk)
        assert body["is_admin_guc"] == "false"
        _assert_request_left_nothing_behind()

    def test_admin_request_sees_all_rows(self, admin_web_user, sample_orders, rls_middleware):
        client = AsyncClient()
        client.force_login(admin_web_user)

        response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["is_admin_guc"] == "true"
        assert body["products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        _assert_request_left_nothing_behind()

    def test_view_exception_still_cleans_up(self, scoped_web_user, rls_middleware):
        client = AsyncClient(raise_request_exception=False)
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/boom/")

        assert response.status_code == 500
        _assert_request_left_nothing_behind()

    def test_view_exception_is_not_masked(self, scoped_web_user, rls_middleware):
        """The view's own exception reaches the caller, not a cleanup ``ValueError``."""
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with pytest.raises(RuntimeError, match="boom"):
            async_to_sync(client.get)("/rls/boom/")
        _assert_request_left_nothing_behind()

    def test_anonymous_request_sets_nothing(self, sample_orders, rls_middleware):
        response = async_to_sync(AsyncClient().get)("/rls/orders/")

        assert response.status_code == 200
        body = response.json()
        assert body["products"] == []
        assert body["tenant_guc"] is None
        assert body["is_admin_guc"] is None
        _assert_request_left_nothing_behind()

    def test_admin_changelist_for_scoped_staff_user(
        self, staff_web_user, sample_orders, rls_middleware
    ):
        """The path the #58 reporter hit: the sync Django admin served over ASGI."""
        client = AsyncClient()
        client.force_login(staff_web_user)

        response = async_to_sync(client.get)("/admin/test_app/order/")

        assert response.status_code == 200
        body = response.content.decode()
        assert "Widget A1" in body
        assert "Gadget B1" not in body
        _assert_request_left_nothing_behind()


async def _read_stream(response):
    return b"".join([chunk async for chunk in response.streaming_content]).decode()


@pytest.mark.integration
class TestNativeAsyncPath:
    """The hand-written ``__acall__`` (#38) end to end, with async views."""

    def test_sync_hooks_are_not_used(self, scoped_web_user, sample_orders, rls_middleware):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with (
            patch.object(RLSTenantMiddleware, "process_request", side_effect=AssertionError),
            patch.object(RLSTenantMiddleware, "process_response", side_effect=AssertionError),
        ):
            response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        assert response.json()["products"] == ["Widget A1", "Widget A2"]
        _assert_request_left_nothing_behind()

    def test_async_view_sees_tenant_rows(
        self, scoped_web_user, sample_orders, tenant_a, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/async-orders/")

        assert response.status_code == 200
        assert response.json() == {
            "products": ["Widget A1", "Widget A2"],
            "database_products": ["Widget A1", "Widget A2"],
            "tenant_id": tenant_a.pk,
            "tenant_guc": str(tenant_a.pk),
            "is_admin_guc": "false",
        }
        _assert_request_left_nothing_behind()

    def test_async_view_for_admin_sees_all_rows(
        self, admin_web_user, sample_orders, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(admin_web_user)

        body = async_to_sync(client.get)("/rls/async-orders/").json()

        assert body["products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        assert body["database_products"] == ["Gadget B1", "Widget A1", "Widget A2"]
        assert body["tenant_id"] is None
        assert body["is_admin_guc"] == "true"
        _assert_request_left_nothing_behind()

    def test_async_view_can_widen_to_admin_and_return(
        self, scoped_web_user, sample_orders, tenant_a, rls_middleware
    ):
        client = AsyncClient()
        client.force_login(scoped_web_user)

        body = async_to_sync(client.get)("/rls/async-widen/").json()

        assert body == {
            "admin": ["Gadget B1", "Widget A1", "Widget A2"],
            "after": ["Widget A1", "Widget A2"],
            "tenant_guc": str(tenant_a.pk),
        }
        _assert_request_left_nothing_behind()

    def test_streaming_body_runs_after_the_request_context(
        self, scoped_web_user, sample_orders, rls_middleware
    ):
        """Same as WSGI: the body is iterated after cleanup, so it needs its own context."""
        client = AsyncClient()
        client.force_login(scoped_web_user)

        response = async_to_sync(client.get)("/rls/async-stream/")
        assert response.streaming is True
        assert async_to_sync(_read_stream)(response) == "0\n2\n"
        _assert_request_left_nothing_behind()

    def test_strict_mode_async_view(self, scoped_web_user, sample_orders, rls_middleware):
        """The request's context satisfies STRICT_MODE in async views; no context raises."""
        # override_settings, not the ``settings`` fixture: that one is restored after
        # ``rls_middleware`` and would bring the RLS MIDDLEWARE back for later tests.
        strict = {**django_settings.RLS_TENANTS, "STRICT_MODE": True}
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with override_settings(RLS_TENANTS=strict):
            assert async_to_sync(client.get)("/rls/async-orders/").status_code == 200
            with pytest.raises(NoTenantContextError):
                async_to_sync(AsyncClient().get)("/rls/async-orders/")
        _assert_request_left_nothing_behind()

    def test_sync_only_middleware_below_runs_the_sync_path(
        self, scoped_web_user, sample_orders, tenant_a
    ):
        """A sync-only middleware after ours makes Django run ours on its sync path."""
        middleware = [*_MIDDLEWARE_WITH_RLS, "tests.rls_views.SyncOnlyMiddleware"]
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with (
            override_settings(MIDDLEWARE=middleware),
            patch.object(RLSTenantMiddleware, "__acall__", side_effect=AssertionError),
        ):
            response = async_to_sync(client.get)("/rls/orders/")

        assert response.status_code == 200
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        assert response.json()["tenant_guc"] == str(tenant_a.pk)
        _assert_request_left_nothing_behind()

    def test_sync_only_middleware_above_keeps_the_native_path(
        self, scoped_web_user, sample_orders, tenant_a
    ):
        """Above ours, a sync-only middleware reaches the native path through async_to_sync."""
        middleware = [*_MIDDLEWARE_WITH_RLS]
        middleware.insert(middleware.index(_RLS), "tests.rls_views.SyncOnlyMiddleware")
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with (
            override_settings(MIDDLEWARE=middleware),
            patch.object(RLSTenantMiddleware, "process_request", side_effect=AssertionError),
        ):
            body = async_to_sync(client.get)("/rls/async-orders/").json()

        assert body["products"] == ["Widget A1", "Widget A2"]
        assert body["database_products"] == ["Widget A1", "Widget A2"]
        assert body["tenant_guc"] == str(tenant_a.pk)
        _assert_request_left_nothing_behind()

    def test_subclass_with_sync_hooks_keeps_them_under_asgi(
        self, scoped_web_user, sample_orders, tenant_a
    ):
        middleware = [
            "tests.rls_views.TaggingRLSTenantMiddleware" if path == _RLS else path
            for path in _MIDDLEWARE_WITH_RLS
        ]
        client = AsyncClient()
        client.force_login(scoped_web_user)

        with override_settings(MIDDLEWARE=middleware):
            response = async_to_sync(client.get)("/rls/async-orders/")

        assert response["X-RLS-Hooks"] == "sync"
        assert response.json()["database_products"] == ["Widget A1", "Widget A2"]
        assert response.json()["tenant_guc"] == str(tenant_a.pk)
        _assert_request_left_nothing_behind()


@pytest.fixture
def guc_probe_404(monkeypatch):
    """Serve 404 pages with ``guc_probe_404_handler``."""
    monkeypatch.setattr(
        admin_urls, "handler404", "tests.rls_views.guc_probe_404_handler", raising=False
    )


@pytest.fixture
def guc_probe_500(monkeypatch):
    """Serve 500 pages with ``guc_probe_500_handler``."""
    monkeypatch.setattr(
        admin_urls, "handler500", "tests.rls_views.guc_probe_500_handler", raising=False
    )


async def _on_one_worker(*fetches):
    """Await each fetch in turn with a one-thread default executor; return the results.

    Django builds error pages in ``thread_sensitive=False`` hops, i.e. on the default
    executor. With one worker, the same thread and connection serve every page, as
    the long-lived worker threads of an ASGI server do.
    """
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    try:
        return [await fetch() for fetch in fetches]
    finally:
        # Django never closes a worker thread's connection; the test does.
        await loop.run_in_executor(None, connections.close_all)


async def _client_get_json(client, path):
    return (await client.get(path)).json()


async def _asgi_get_json(application, path, cookie=None):
    """Send one GET through ``application`` as an ASGI server would; return the JSON body."""
    headers = [(b"host", b"testserver")]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if not messages:
            # Never disconnect: ASGIHandler cancels this wait once the response is sent.
            await asyncio.Event().wait()
        return messages.pop()

    body = []

    async def send(message):
        if message["type"] == "http.response.body":
            body.append(message.get("body", b""))

    await application(scope, receive, send)
    return json.loads(b"".join(body))


def _serve_like_an_asgi_server(application, requests):
    """Serve each ``(path, cookie)`` in turn on a fresh event-loop thread; return the bodies.

    ``asyncio.run`` on a thread of its own, as an ASGI server runs its loop: no
    synchronous caller serves the thread-sensitive hops, so ``ASGIHandler`` gives
    each request a thread of its own. ``AsyncClient`` driven from the test runs
    them on the test thread instead.
    """
    fetches = [partial(_asgi_get_json, application, path, cookie) for path, cookie in requests]
    with ThreadPoolExecutor(max_workers=1) as loop_thread:
        return loop_thread.submit(asyncio.run, _on_one_worker(*fetches)).result()


_HOOK_SUBCLASS = "tests.rls_views.TaggingRLSTenantMiddleware"

# Each way Django can run the RLS middleware under ASGI, and so each place that
# records which thread set the GUCs.
_ASGI_MIDDLEWARE_PATHS = {
    "native": _MIDDLEWARE_WITH_RLS,
    "sync-path": [*_MIDDLEWARE_WITH_RLS, "tests.rls_views.SyncOnlyMiddleware"],
    "hook-path": [_HOOK_SUBCLASS if path == _RLS else path for path in _MIDDLEWARE_WITH_RLS],
}


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
class TestErrorPageOnWorkerThread:
    """#130, #41: an error page built on a worker thread runs in the request's context.

    The worker thread's connection gets the request's GUCs from the GUC affinity
    guard, so the page sees what the request may see, and keeps them only until
    its next statement: the anonymous page built after it on the same thread and
    connection sees nothing.

    ``transaction=True``: the worker thread's connection is not the test's, so it
    sees committed rows only.
    """

    @pytest.mark.parametrize("middleware_path", list(_ASGI_MIDDLEWARE_PATHS))
    @pytest.mark.parametrize("first_user", ["scoped", "admin"])
    def test_unresolved_url_inherits_the_context_and_leaves_none_behind(
        self,
        first_user,
        middleware_path,
        scoped_web_user,
        admin_web_user,
        sample_orders,
        tenant_a,
        enforce_rls_on_other_threads,
        guc_probe_404,
    ):
        """The URL is resolved outside ``process_exception``'s reach: the context is live."""
        authenticated = AsyncClient()
        authenticated.force_login({"scoped": scoped_web_user, "admin": admin_web_user}[first_user])

        with override_settings(MIDDLEWARE=_ASGI_MIDDLEWARE_PATHS[middleware_path]):
            first, second = async_to_sync(_on_one_worker)(
                partial(_client_get_json, authenticated, "/no-such-page/"),
                partial(_client_get_json, AsyncClient(), "/no-such-page/"),
            )

        # One worker thread and connection built both pages, not the test thread, and
        # the first one ran in a copy of the live request context.
        assert first["thread"] == second["thread"] != threading.get_ident()
        assert first["pid"] == second["pid"]
        assert [page["rls_context_active"] for page in (first, second)] == [True, False]
        expected_first = {
            "scoped": (str(tenant_a.pk), "false", ["Widget A1", "Widget A2"]),
            "admin": (None, "true", ["Gadget B1", "Widget A1", "Widget A2"]),
        }[first_user]
        assert (
            first["tenant_guc"],
            first["is_admin_guc"],
            first["database_products"],
        ) == expected_first
        assert (second["tenant_guc"], second["is_admin_guc"], second["database_products"]) == (
            None,
            None,
            [],
        )
        _assert_request_left_nothing_behind()

    @pytest.mark.parametrize("middleware_path", list(_ASGI_MIDDLEWARE_PATHS))
    def test_500_page_runs_without_a_block_the_view_left_open(
        self,
        middleware_path,
        scoped_web_user,
        sample_orders,
        enforce_rls_on_other_threads,
        guc_probe_500,
    ):
        """No ``process_exception`` here: ``got_request_exception`` ends the block (#41)."""
        client = AsyncClient(raise_request_exception=False)
        client.force_login(scoped_web_user)

        with override_settings(MIDDLEWARE=_ASGI_MIDDLEWARE_PATHS[middleware_path]):
            (page,) = async_to_sync(_on_one_worker)(
                partial(_client_get_json, client, "/rls/async-left-open/"),
            )

        assert page["thread"] != threading.get_ident()
        assert (page["tenant_guc"], page["is_admin_guc"], page["database_products"]) == (
            None,
            None,
            [],
        )

    @pytest.mark.parametrize("middleware_path", list(_ASGI_MIDDLEWARE_PATHS))
    def test_through_asgi_handler(
        self,
        middleware_path,
        scoped_web_user,
        sample_orders,
        tenant_a,
        enforce_rls_on_other_threads,
        guc_probe_404,
    ):
        """The same through ``ASGIHandler``, which serves each request on a thread of its own.

        That thread sets the GUCs, so a connection it reopens mid-request gets them.
        """
        client = Client()
        client.force_login(scoped_web_user)
        session = client.cookies[django_settings.SESSION_COOKIE_NAME].value
        cookie = f"{django_settings.SESSION_COOKIE_NAME}={session}"

        with override_settings(MIDDLEWARE=_ASGI_MIDDLEWARE_PATHS[middleware_path]):
            reopened, first, second = _serve_like_an_asgi_server(
                ASGIHandler(),
                [
                    ("/rls/reconnect/", cookie),
                    ("/no-such-page/", cookie),
                    ("/no-such-page/", None),
                ],
            )

        tenant_a_view = {
            "tenant_guc": str(tenant_a.pk),
            "is_admin_guc": "false",
            "database_products": ["Widget A1", "Widget A2"],
        }
        assert reopened == tenant_a_view
        assert first["pid"] == second["pid"]
        assert [page["rls_context_active"] for page in (first, second)] == [True, False]
        assert {key: first[key] for key in tenant_a_view} == tenant_a_view
        assert (second["tenant_guc"], second["is_admin_guc"], second["database_products"]) == (
            None,
            None,
            [],
        )
        _assert_request_left_nothing_behind()
