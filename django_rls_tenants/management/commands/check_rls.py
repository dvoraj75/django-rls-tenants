"""Management command: check_rls.

Verifies that all RLSProtectedModel subclasses have the expected
RLS policies applied in the database. Reports missing or stale policies.
"""

from __future__ import annotations
