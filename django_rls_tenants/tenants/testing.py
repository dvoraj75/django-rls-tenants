"""Test utilities for django-rls-tenants.

Provides ``rls_bypass``, ``rls_as_tenant`` context managers and
assertion helpers (``assert_rls_applied``, ``assert_rls_bypassed``)
for use in test suites.
"""

from __future__ import annotations
