"""Tenant-aware context managers.

Provides ``tenant_context``, ``admin_context``, and the ``with_rls_context``
decorator for setting the active tenant in PostgreSQL GUC variables.
All support save/restore nesting.
"""

from __future__ import annotations
