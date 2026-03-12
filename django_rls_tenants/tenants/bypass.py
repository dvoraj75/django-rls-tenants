"""Bypass flag helpers for temporarily disabling RLS enforcement.

Provides ``set_bypass_flag``, ``clear_bypass_flag``, and re-exports
the generic ``bypass_flag`` context manager from ``rls.context``.
"""

from __future__ import annotations
