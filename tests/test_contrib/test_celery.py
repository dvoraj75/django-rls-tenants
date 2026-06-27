"""Tests for django_rls_tenants.contrib.celery (issue #32).

These run Celery in eager mode (``task_always_eager``), so no broker or worker is
needed: a task executes inline when enqueued, while still exercising the real
header capture (``apply_async`` / ``apply``) and restore (``__call__``) paths.

The suite splits into:

* pure-logic unit tests for ``_capture`` / ``_request_context`` / ``_merge_headers``
  and the signal handlers (no row access);
* eager round-trip tests proving an ``@rls_task`` body runs under the propagated
  ``tenant_context()`` / ``admin_context()``, including across a chain and a group;
* one ``@pytest.mark.integration`` test proving end-to-end row isolation under
  ``enforce_rls``.

Entering a real ``tenant_context()`` issues ``set_config`` GUC statements, so the
tests that restore a context need a database connection (the autouse cleanup
fixture in ``tests/conftest.py`` already pulls in ``db``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from celery import Celery, chain, chord, group, shared_task
from celery.signals import before_task_publish

from django_rls_tenants.contrib import celery as celery_mod
from django_rls_tenants.contrib.celery import (
    _ADMIN_HEADER,
    _TENANT_HEADER,
    RLSTask,
    _capture,
    _merge_headers,
    _request_context,
    install,
    rls_task,
    uninstall,
)
from django_rls_tenants.exceptions import NoTenantContextError
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    get_rls_context_active,
    set_current_tenant_id,
    set_rls_context_active,
)

# A dedicated eager Celery app. ``task_eager_propagates`` re-raises task
# exceptions to the caller (so a failing task surfaces in the test);
# ``task_store_eager_result`` makes ``.get()`` return the value.
celery_app = Celery("test_rls_tenants")
celery_app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
    task_store_eager_result=True,
    broker_url="memory://",
    result_backend="cache+memory://",
)
celery_app.set_current()


@rls_task
def report_context() -> tuple[object, bool]:
    """Return the RLS context the worker established for this task."""
    return get_current_tenant_id(), get_rls_context_active()


@rls_task
def seed_task() -> object:
    """First step of a canvas; returns the active tenant id."""
    return get_current_tenant_id()


@rls_task
def follow_task(_prev: object = None) -> object:
    """Downstream canvas step; returns the tenant id it inherited."""
    return get_current_tenant_id()


class StrictRLSTask(RLSTask):
    """RLSTask variant that refuses to run without a propagated context."""

    rls_require_context = True


@shared_task(base=StrictRLSTask)
def strict_task() -> str:
    """Task that must always run inside an RLS context."""
    return "ran"


@rls_task
def fetch_products() -> list[str]:
    """Return the products visible to the worker (RLS-scoped)."""
    from tests.test_app.models import Order  # noqa: PLC0415

    return sorted(Order.objects.values_list("product", flat=True))


@rls_task(bind=True)
def report_headers(self) -> dict[str, object]:
    """Return the RLS headers this task was enqueued with.

    Bound so it can read ``self.request.headers``. Used to inspect the
    *propagated header* directly, which proves the capture path independently
    of the body's observed context (eager mode masks the latter).
    """
    headers = self.request.headers or {}
    return {_TENANT_HEADER: headers.get(_TENANT_HEADER), _ADMIN_HEADER: headers.get(_ADMIN_HEADER)}


# ---------------------------------------------------------------------------
# _capture()
# ---------------------------------------------------------------------------


class TestCapture:
    """_capture() reads live context first, then inherits from the running task."""

    def test_live_tenant_context(self):
        """A live tenant context is captured as a tenant header."""
        set_current_tenant_id(7)
        assert _capture() == {_TENANT_HEADER: 7, _ADMIN_HEADER: False}

    def test_live_admin_context(self):
        """A live admin context (active flag, no tenant) is captured as admin."""
        set_current_tenant_id(None)
        set_rls_context_active(True)
        assert _capture() == {_TENANT_HEADER: None, _ADMIN_HEADER: True}

    def test_no_context_is_empty(self):
        """No context and no running task captures nothing (fail-closed)."""
        assert _capture() == {}

    def test_inherits_tenant_from_current_task(self, monkeypatch):
        """With no live context, headers are inherited from the running task."""
        fake = SimpleNamespace(
            request=SimpleNamespace(headers={_TENANT_HEADER: 42, _ADMIN_HEADER: False})
        )
        monkeypatch.setattr(celery_mod, "current_task", fake)
        assert _capture() == {_TENANT_HEADER: 42, _ADMIN_HEADER: False}

    def test_inherits_admin_from_current_task(self, monkeypatch):
        """Admin context propagates through the current-task fallback too."""
        fake = SimpleNamespace(
            request=SimpleNamespace(headers={_TENANT_HEADER: None, _ADMIN_HEADER: True})
        )
        monkeypatch.setattr(celery_mod, "current_task", fake)
        assert _capture() == {_TENANT_HEADER: None, _ADMIN_HEADER: True}

    def test_current_task_without_rls_headers(self, monkeypatch):
        """A running task whose headers lack RLS keys contributes nothing."""
        fake = SimpleNamespace(request=SimpleNamespace(headers={"other": 1}))
        monkeypatch.setattr(celery_mod, "current_task", fake)
        assert _capture() == {}

    def test_live_context_beats_current_task(self, monkeypatch):
        """A live tenant context wins over the running task's headers."""
        set_current_tenant_id(7)
        fake = SimpleNamespace(request=SimpleNamespace(headers={_TENANT_HEADER: 42}))
        monkeypatch.setattr(celery_mod, "current_task", fake)
        assert _capture() == {_TENANT_HEADER: 7, _ADMIN_HEADER: False}

    def test_zero_tenant_id_is_captured(self):
        """tenant_id=0 is falsy but not None -- it must be captured, not skipped."""
        set_current_tenant_id(0)
        assert _capture() == {_TENANT_HEADER: 0, _ADMIN_HEADER: False}


# ---------------------------------------------------------------------------
# _request_context()
# ---------------------------------------------------------------------------


class TestRequestContext:
    """_request_context() parses the task request's headers."""

    def test_reads_tenant_from_headers(self):
        req = SimpleNamespace(headers={_TENANT_HEADER: 5, _ADMIN_HEADER: False})
        assert _request_context(req) == (5, False)

    def test_reads_admin_from_headers(self):
        req = SimpleNamespace(headers={_TENANT_HEADER: None, _ADMIN_HEADER: True})
        assert _request_context(req) == (None, True)

    def test_headers_without_rls_keys(self):
        """Headers present but missing the tenant key -> no context."""
        req = SimpleNamespace(headers={"other": 1})
        assert _request_context(req) == (None, False)

    def test_falls_back_to_top_level_attributes(self):
        """When headers is not a dict, top-level request attrs are used."""
        req = SimpleNamespace(headers=None, rls_tenant_id=9, rls_admin=False)
        assert _request_context(req) == (9, False)

    def test_none_request(self):
        assert _request_context(None) == (None, False)

    def test_tenant_header_without_admin_key_defaults_admin_false(self):
        """A tenant header with no admin key defaults is_admin to False."""
        req = SimpleNamespace(headers={_TENANT_HEADER: 5})
        assert _request_context(req) == (5, False)


# ---------------------------------------------------------------------------
# _merge_headers()
# ---------------------------------------------------------------------------


class TestMergeHeaders:
    """_merge_headers() folds the captured context into outgoing headers."""

    def test_no_context_returns_empty(self):
        options = {}
        assert _merge_headers(options) == {}

    def test_injects_live_tenant(self):
        set_current_tenant_id(7)
        assert _merge_headers({}) == {_TENANT_HEADER: 7, _ADMIN_HEADER: False}

    def test_does_not_clobber_explicit_header(self):
        """An explicitly-passed header is never overwritten by capture."""
        set_current_tenant_id(7)
        options = {"headers": {_TENANT_HEADER: 99}}
        assert _merge_headers(options)[_TENANT_HEADER] == 99

    def test_pops_headers_and_keeps_other_options(self):
        """``headers`` is removed from options; other options are untouched."""
        options = {"headers": {"x": 1}, "countdown": 5}
        merged = _merge_headers(options)
        assert "headers" not in options
        assert options == {"countdown": 5}
        assert merged["x"] == 1


# ---------------------------------------------------------------------------
# rls_task() / RLSTask base wiring
# ---------------------------------------------------------------------------


class TestRlsTaskDecorator:
    """rls_task() is shared_task with base defaulted to RLSTask."""

    def test_task_uses_rlstask_base(self):
        assert isinstance(report_context, RLSTask)

    def test_explicit_base_is_respected(self):
        """A custom base (StrictRLSTask) passes through rls_task's setdefault."""
        assert isinstance(strict_task, StrictRLSTask)
        assert strict_task.rls_require_context is True

    def test_default_does_not_require_context(self):
        assert report_context.rls_require_context is False


# ---------------------------------------------------------------------------
# Capture wiring: apply_async / apply fold the live context into the headers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCaptureWiring:
    """apply_async/apply actually write the live context into the message headers.

    Eager mode runs the body synchronously inside the enqueuing context, so a
    test that only checks the body's observed tenant cannot distinguish capture
    from the outer ``tenant_context`` leaking in. Inspecting the propagated
    header (via a bound task reading ``self.request.headers``) isolates the
    capture path: the header is present only because apply_async/apply ran it.
    """

    def test_apply_async_captures_live_tenant_into_headers(self, tenant_a):
        """Enqueuing inside a tenant context writes the tenant header."""
        with tenant_context(tenant_a.pk):
            headers = report_headers.delay().get()
        assert headers == {_TENANT_HEADER: tenant_a.pk, _ADMIN_HEADER: False}

    def test_apply_async_captures_admin_into_headers(self):
        """Enqueuing inside an admin context writes the admin header."""
        with admin_context():
            headers = report_headers.delay().get()
        assert headers == {_TENANT_HEADER: None, _ADMIN_HEADER: True}

    def test_explicit_header_survives_eager_double_capture(self):
        """Eager apply_async re-invokes apply; an explicit header must survive both."""
        headers = report_headers.apply_async(headers={_TENANT_HEADER: 99}).get()
        assert headers[_TENANT_HEADER] == 99

    def test_zero_tenant_id_enters_tenant_context(self):
        """A 0 tenant header enters tenant_context(0), not admin/unscoped."""
        result = report_context.apply_async(
            headers={_TENANT_HEADER: 0, _ADMIN_HEADER: False}
        ).get()
        assert result == (0, True)


# ---------------------------------------------------------------------------
# Eager round-trip: __call__ restores the context
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEagerRoundTrip:
    """An enqueued @rls_task body runs under the propagated context."""

    def test_restores_tenant_from_explicit_header(self, tenant_a):
        """No outer context: the body is scoped purely from the task header."""
        assert get_current_tenant_id() is None
        result = report_context.apply_async(headers={_TENANT_HEADER: tenant_a.pk}).get()
        assert result == (tenant_a.pk, True)
        assert get_current_tenant_id() is None  # restored on exit

    def test_restores_admin_from_header(self):
        """A header marking admin context runs the body in admin mode."""
        result = report_context.apply_async(
            headers={_TENANT_HEADER: None, _ADMIN_HEADER: True}
        ).get()
        assert result == (None, True)
        assert get_rls_context_active() is False

    def test_captures_live_context_on_enqueue(self, tenant_a):
        """Enqueuing inside tenant_context() scopes the worker body to it."""
        with tenant_context(tenant_a.pk):
            result = report_context.delay().get()
        assert result == (tenant_a.pk, True)

    def test_no_context_runs_unscoped(self):
        """Without context (and without rls_require_context) the body just runs."""
        result = report_context.delay().get()
        assert result == (None, False)

    def test_exception_in_body_restores_context(self, tenant_a):
        """A failing body still unwinds the tenant context (try/finally)."""

        @rls_task
        def boom() -> None:
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            boom.apply_async(headers={_TENANT_HEADER: tenant_a.pk}).get()
        assert get_current_tenant_id() is None

    def test_bound_task_propagates_and_restores(self, tenant_a):
        """@rls_task(bind=True) receives self and still runs under the context."""

        @rls_task(bind=True)
        def bound_report(self) -> tuple[object, bool, str]:
            return get_current_tenant_id(), get_rls_context_active(), self.name

        assert get_current_tenant_id() is None
        tenant_id, active, name = bound_report.apply_async(
            headers={_TENANT_HEADER: tenant_a.pk, _ADMIN_HEADER: False}
        ).get()
        assert (tenant_id, active) == (tenant_a.pk, True)
        assert "bound_report" in name
        assert get_current_tenant_id() is None  # restored


# ---------------------------------------------------------------------------
# rls_require_context fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRequireContext:
    """rls_require_context=True turns a missing context into an error."""

    def test_raises_without_context(self):
        with pytest.raises(NoTenantContextError, match="without an RLS context"):
            strict_task.apply_async().get()

    def test_error_carries_hint(self):
        from django_rls_tenants.tenants.errors import HINT_NO_CONTEXT  # noqa: PLC0415

        with pytest.raises(NoTenantContextError) as exc_info:
            strict_task.apply_async().get()
        assert exc_info.value.hint == HINT_NO_CONTEXT

    def test_runs_with_context(self, tenant_a):
        result = strict_task.apply_async(headers={_TENANT_HEADER: tenant_a.pk}).get()
        assert result == "ran"

    def test_runs_with_admin_header(self):
        """An admin header satisfies rls_require_context=True (does not raise)."""
        result = strict_task.apply_async(headers={_TENANT_HEADER: None, _ADMIN_HEADER: True}).get()
        assert result == "ran"


# ---------------------------------------------------------------------------
# Canvas propagation (chains / groups)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCanvasPropagation:
    """Context flows across chains and groups of RLS tasks."""

    def test_chain_propagates_tenant(self, tenant_a):
        with tenant_context(tenant_a.pk):
            result = chain(seed_task.s(), follow_task.s()).apply_async()
        assert result.get() == tenant_a.pk

    def test_group_propagates_tenant(self, tenant_a):
        with tenant_context(tenant_a.pk):
            result = group(seed_task.s(), seed_task.s()).apply_async()
        assert result.get() == [tenant_a.pk, tenant_a.pk]

    def test_chord_propagates_tenant(self, tenant_a):
        """A chord's callback runs under the propagated tenant context."""
        with tenant_context(tenant_a.pk):
            result = chord([seed_task.s(), seed_task.s()])(follow_task.s())
        assert result.get() == tenant_a.pk


# ---------------------------------------------------------------------------
# Signal-based install() / uninstall()
# ---------------------------------------------------------------------------


class TestPublishSignal:
    """before_task_publish injects the captured context into outgoing headers."""

    def test_injects_into_headers(self):
        set_current_tenant_id(7)
        headers: dict[str, object] = {}
        celery_mod._before_task_publish(headers=headers)
        assert headers == {_TENANT_HEADER: 7, _ADMIN_HEADER: False}

    def test_none_headers_is_noop(self):
        """Protocol v1 (no headers mapping) is tolerated."""
        set_current_tenant_id(7)
        assert celery_mod._before_task_publish(headers=None) is None

    def test_does_not_clobber_existing(self):
        set_current_tenant_id(7)
        headers: dict[str, object] = {_TENANT_HEADER: 99}
        celery_mod._before_task_publish(headers=headers)
        assert headers[_TENANT_HEADER] == 99


@pytest.mark.django_db
class TestPrerunPostrunSignals:
    """task_prerun/task_postrun enter and exit the context on the worker."""

    def test_enter_and_exit_tenant(self, tenant_a):
        task = SimpleNamespace(
            request=SimpleNamespace(headers={_TENANT_HEADER: tenant_a.pk, _ADMIN_HEADER: False})
        )
        assert get_current_tenant_id() is None
        celery_mod._task_prerun(task_id="t1", task=task)
        assert get_current_tenant_id() == tenant_a.pk
        celery_mod._task_postrun(task_id="t1")
        assert get_current_tenant_id() is None

    def test_enter_and_exit_admin(self):
        task = SimpleNamespace(
            request=SimpleNamespace(headers={_TENANT_HEADER: None, _ADMIN_HEADER: True})
        )
        celery_mod._task_prerun(task_id="t2", task=task)
        assert get_rls_context_active() is True
        celery_mod._task_postrun(task_id="t2")
        assert get_rls_context_active() is False

    def test_skips_rlstask_instances(self):
        """RLSTask tasks manage their own context, so the signal skips them."""
        celery_mod._task_prerun(task_id="t3", task=RLSTask())
        assert "t3" not in celery_mod._signal_contexts

    def test_no_context_does_not_register(self):
        task = SimpleNamespace(request=SimpleNamespace(headers={}))
        celery_mod._task_prerun(task_id="t4", task=task)
        assert "t4" not in celery_mod._signal_contexts

    def test_postrun_without_prerun_is_safe(self):
        """Postrun with no recorded context is a no-op, not an error."""
        celery_mod._task_postrun(task_id="never-entered")
        celery_mod._task_postrun(task_id=None)

    def test_reentrant_task_id_unwinds_lifo(self, tenant_a, tenant_b):
        """Two prerun entries under one task id unwind innermost-first."""
        task_a = SimpleNamespace(request=SimpleNamespace(headers={_TENANT_HEADER: tenant_a.pk}))
        task_b = SimpleNamespace(request=SimpleNamespace(headers={_TENANT_HEADER: tenant_b.pk}))
        celery_mod._task_prerun(task_id="dup", task=task_a)
        celery_mod._task_prerun(task_id="dup", task=task_b)
        assert get_current_tenant_id() == tenant_b.pk
        celery_mod._task_postrun(task_id="dup")  # pops B; A still on the stack
        assert get_current_tenant_id() == tenant_a.pk
        celery_mod._task_postrun(task_id="dup")  # pops A; entry removed
        assert get_current_tenant_id() is None
        assert "dup" not in celery_mod._signal_contexts

    def test_uninstall_exits_inflight_contexts(self, tenant_a):
        """uninstall() unwinds a context still open from prerun (no leak)."""
        task = SimpleNamespace(request=SimpleNamespace(headers={_TENANT_HEADER: tenant_a.pk}))
        celery_mod._task_prerun(task_id="leak", task=task)
        assert get_current_tenant_id() == tenant_a.pk  # entered, not yet exited
        uninstall()
        assert get_current_tenant_id() is None  # exited by uninstall's cleanup
        assert celery_mod._signal_contexts == {}

    def test_prerun_swallows_enter_failure(self, monkeypatch):
        """A failure entering the context is logged, not raised; task not registered."""

        class BoomCtx:
            def __enter__(self):
                raise RuntimeError("db down")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(celery_mod, "tenant_context", lambda *a, **k: BoomCtx())
        task = SimpleNamespace(request=SimpleNamespace(headers={_TENANT_HEADER: 5}))
        celery_mod._task_prerun(task_id="boom", task=task)  # must not raise
        assert "boom" not in celery_mod._signal_contexts

    def test_postrun_swallows_exit_failure(self):
        """A failure exiting the context is swallowed; the entry is still cleared."""

        class BoomExit:
            def __exit__(self, *exc):
                raise RuntimeError("connection lost")

        celery_mod._signal_contexts["x"] = [BoomExit()]
        celery_mod._task_postrun(task_id="x")  # must not raise
        assert "x" not in celery_mod._signal_contexts


class TestInstallUninstall:
    """install() wires the publish signal; uninstall() removes it."""

    def test_install_connects_and_uninstall_disconnects(self):
        uninstall()  # ensure a clean baseline
        set_current_tenant_id(7)

        before: dict[str, object] = {}
        before_task_publish.send(sender="rls-test", headers=before)
        assert before == {}  # not connected yet

        install()
        try:
            during: dict[str, object] = {}
            before_task_publish.send(sender="rls-test", headers=during)
            assert during == {_TENANT_HEADER: 7, _ADMIN_HEADER: False}
        finally:
            uninstall()

        after: dict[str, object] = {}
        before_task_publish.send(sender="rls-test", headers=after)
        assert after == {}  # disconnected again

    def test_install_is_idempotent(self):
        install()
        install()
        try:
            set_current_tenant_id(7)
            headers: dict[str, object] = {}
            before_task_publish.send(sender="rls-test", headers=headers)
            assert headers == {_TENANT_HEADER: 7, _ADMIN_HEADER: False}
        finally:
            uninstall()

    def test_uninstall_is_safe_without_install(self):
        uninstall()
        uninstall()


# ---------------------------------------------------------------------------
# End-to-end isolation (live RLS enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
class TestTaskIsolation:
    """An @rls_task only sees its tenant's rows once RLS is enforced."""

    def test_tenant_task_sees_only_its_rows(self, enforce_rls, sample_orders, tenant_a):
        result = fetch_products.apply_async(headers={_TENANT_HEADER: tenant_a.pk}).get()
        assert result == ["Widget A1", "Widget A2"]

    def test_admin_task_sees_all_rows(self, enforce_rls, sample_orders):
        result = fetch_products.apply_async(
            headers={_TENANT_HEADER: None, _ADMIN_HEADER: True}
        ).get()
        assert result == ["Gadget B1", "Widget A1", "Widget A2"]
