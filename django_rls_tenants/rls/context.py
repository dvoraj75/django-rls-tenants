"""Generic RLS context managers.

Provides ``rls_context`` for setting/clearing arbitrary GUC variables,
and ``bypass_flag`` for toggling boolean bypass flags within a
transaction-scoped context. Both support save/restore nesting.
"""

from __future__ import annotations
