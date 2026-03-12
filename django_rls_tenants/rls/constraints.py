"""RLSConstraint -- a Django BaseConstraint for RLS policy management.

Generates ``CREATE POLICY`` / ``DROP POLICY`` SQL during migrations,
with idempotency checks, ``FORCE ROW LEVEL SECURITY``, and configurable
bypass flags.
"""

from __future__ import annotations
