"""Generic PostgreSQL Row-Level Security primitives.

This layer has **zero imports** from ``django_rls_tenants.tenants``.
It provides reusable building blocks: GUC variable helpers, a migration-aware
``RLSConstraint``, and generic context managers.
"""

from __future__ import annotations

# Public re-exports will be added as modules are implemented.
# See plan/implementation-plan.md Phase 1, Step 1.4.
