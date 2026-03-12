"""RLSTenantMiddleware -- sets tenant context from the authenticated user.

Reads ``request.user.rls_tenant_id`` and activates a ``tenant_context``
for the duration of the request. Clears context in a ``finally`` block.
"""

from __future__ import annotations
