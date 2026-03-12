"""Tenant-aware QuerySet and Manager.

``TenantQuerySet`` provides a custom ``_fetch_all`` to ensure the GUC
variable is set at evaluation time (fixing the lazy queryset bug).
``RLSManager`` is the default manager for ``RLSProtectedModel``.
"""

from __future__ import annotations
