"""Tests for thread-safety and ContextVar isolation.

Verifies that concurrent threads each maintain independent tenant
context via ``contextvars.ContextVar``, preventing cross-tenant
data leaks in WSGI deployments with thread pools.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from django_rls_tenants.tenants.middleware import (
    _clear_gucs_set_flag,
    _mark_gucs_set,
    _were_gucs_set,
)
from django_rls_tenants.tenants.state import (
    get_current_tenant_id,
    reset_current_tenant_id,
    set_current_tenant_id,
)


class TestContextVarThreadIsolation:
    """Verify ContextVar provides per-thread tenant isolation."""

    def test_two_threads_see_own_tenant_id(self):
        """Two concurrent threads each see only their own tenant ID."""
        barrier = threading.Barrier(2)
        results = {}

        def worker(tenant_id, name):
            token = set_current_tenant_id(tenant_id)
            try:
                # Synchronize: both threads have set their tenant ID
                barrier.wait(timeout=5)
                # Each thread should see only its own value
                results[name] = get_current_tenant_id()
            finally:
                reset_current_tenant_id(token)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(worker, 100, "thread_a"),
                executor.submit(worker, 200, "thread_b"),
            ]
            for f in as_completed(futures):
                f.result()  # propagate exceptions

        assert results["thread_a"] == 100
        assert results["thread_b"] == 200

    def test_main_thread_unaffected_by_worker(self):
        """Setting tenant ID in a worker thread does not affect the main thread."""
        assert get_current_tenant_id() is None

        barrier = threading.Barrier(2)

        def worker():
            token = set_current_tenant_id(999)
            try:
                barrier.wait(timeout=5)
            finally:
                reset_current_tenant_id(token)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker)
            barrier.wait(timeout=5)
            # Main thread should still see None
            assert get_current_tenant_id() is None
            future.result()

        assert get_current_tenant_id() is None

    def test_nesting_isolated_across_threads(self):
        """Nested set/reset in one thread does not affect another."""
        barrier = threading.Barrier(2)
        results = {}

        def worker_nested(name):
            token_outer = set_current_tenant_id(1)
            try:
                token_inner = set_current_tenant_id(2)
                try:
                    barrier.wait(timeout=5)
                    results[f"{name}_inner"] = get_current_tenant_id()
                finally:
                    reset_current_tenant_id(token_inner)
                results[f"{name}_outer"] = get_current_tenant_id()
            finally:
                reset_current_tenant_id(token_outer)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(worker_nested, "a"),
                executor.submit(worker_nested, "b"),
            ]
            for f in as_completed(futures):
                f.result()

        assert results["a_inner"] == 2
        assert results["a_outer"] == 1
        assert results["b_inner"] == 2
        assert results["b_outer"] == 1

    def test_many_threads_no_cross_contamination(self):
        """10 concurrent threads each maintain their own tenant context."""
        num_threads = 10
        barrier = threading.Barrier(num_threads)
        results = {}

        def worker(tenant_id):
            token = set_current_tenant_id(tenant_id)
            try:
                barrier.wait(timeout=10)
                # Read back after all threads have set their values
                results[tenant_id] = get_current_tenant_id()
            finally:
                reset_current_tenant_id(token)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for f in as_completed(futures):
                f.result()

        for i in range(num_threads):
            assert results[i] == i, f"Thread {i} saw tenant {results[i]} instead of {i}"


class TestGucsSetFlagThreadIsolation:
    """Verify the GUC-set flag is isolated across threads.

    After Fix 5, this flag uses ``ContextVar`` instead of
    ``threading.local``, providing proper per-context isolation.
    """

    def test_flag_isolated_between_threads(self):
        """GUC-set flag in one thread does not leak to another."""
        barrier = threading.Barrier(2)
        results = {}

        def setter():
            _mark_gucs_set()
            barrier.wait(timeout=5)
            results["setter"] = _were_gucs_set()
            _clear_gucs_set_flag()

        def reader():
            _clear_gucs_set_flag()
            barrier.wait(timeout=5)
            results["reader"] = _were_gucs_set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(setter),
                executor.submit(reader),
            ]
            for f in as_completed(futures):
                f.result()

        assert results["setter"] is True
        assert results["reader"] is False
