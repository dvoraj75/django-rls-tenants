"""``TenantUser`` protocol -- the interface user models must satisfy.

Any object with ``is_tenant_admin`` and ``rls_tenant_id`` properties
can be used with the library's context managers, managers, and middleware.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TenantUser(Protocol):
    """Protocol that user objects must satisfy for RLS context resolution.

    Implement these two properties on your User model::

        class User(AbstractUser, RLSProtectedModel):
            @property
            def is_tenant_admin(self) -> bool:
                return self.role.name == "ADMIN"

            @property
            def rls_tenant_id(self) -> int | str | None:
                return self.tenant_id if self.tenant_id else None
    """

    @property
    def is_tenant_admin(self) -> bool:
        """Return ``True`` if this user bypasses RLS (super-admin)."""
        ...

    @property
    def rls_tenant_id(self) -> int | str | None:
        """Return the tenant ID for RLS filtering, or ``None`` for admins."""
        ...
