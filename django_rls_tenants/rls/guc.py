"""PostgreSQL GUC (Grand Unified Configuration) variable helpers.

Provides ``set_guc``, ``get_guc``, and ``clear_guc`` for managing session-level
or transaction-local configuration parameters used by RLS policies.
"""

from __future__ import annotations
