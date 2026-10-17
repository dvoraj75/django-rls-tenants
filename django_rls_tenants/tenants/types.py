"""``TenantUser`` protocol -- the interface user models must satisfy.

Any object with ``is_tenant_admin`` and ``rls_tenant_id`` properties
can be used with the library's context managers, managers, and middleware.
A user who belongs to several tenants also has ``rls_tenant_ids``
(``MultiTenantUser``) and selects one of them per request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Collection


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


@runtime_checkable
class MultiTenantUser(TenantUser, Protocol):
    """A ``TenantUser`` who belongs to several tenants and selects one per request.

    Add ``rls_tenant_ids`` to the user model; ``rls_tenant_id`` stays the
    tenant a request runs in when it selects none (``None`` for no default)::

        class User(AbstractUser):
            @cached_property
            def rls_tenant_ids(self) -> frozenset[int]:
                return frozenset(self.memberships.values_list("tenant_id", flat=True))

    ``RLSTenantMiddleware``, ``RLSTenantAPIViewMixin`` and
    ``RLSTenantModelAdmin`` then run the request of such a user (one who is
    not a tenant admin) in the tenant it selects, with the
    ``RLS_TENANTS["TENANT_HEADER"]`` header or with
    :func:`~django_rls_tenants.tenants.selection.switch_tenant`, once they
    have checked that it is ``rls_tenant_id`` or one of ``rls_tenant_ids``.
    ``isinstance(user, MultiTenantUser)`` does not read the property.
    """

    @property
    def rls_tenant_ids(self) -> Collection[int | str]:
        """Return the IDs of the tenants this user may select (``rls_tenant_id`` always may be).

        Read only when a request selects a tenant, by
        ``RLSTenantPermission``, and by the admin's tenant switcher, possibly
        more than once per request: cache it on the user object
        (``cached_property``).
        """
        ...
