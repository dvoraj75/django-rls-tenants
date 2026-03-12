"""Layering enforcement test.

Verifies that the ``rls/`` layer has zero imports from ``tenants/``.
This maintains the clean internal architecture where generic RLS primitives
are independent of the multitenancy layer.
"""

from __future__ import annotations
