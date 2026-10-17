from __future__ import annotations

from functools import cached_property

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="users",
    )
    is_tenant_admin = models.BooleanField(default=False)

    @property
    def rls_tenant_id(self) -> int | None:
        # The tenant a request runs in when it selects none.
        return self.tenant_id  # type: ignore[return-value]

    @cached_property
    def rls_tenant_ids(self) -> frozenset[int]:
        # MultiTenantUser: the other tenants this user may select (switch_tenant(),
        # or the X-Tenant header). Cached: a request reads it more than once.
        return frozenset(self.memberships.values_list("tenant_id", flat=True))

    def __str__(self) -> str:
        label: str = self.email or self.username
        if self.tenant:
            return f"{label} ({self.tenant.name})"
        return f"{label} (admin)"


class Membership(models.Model):
    """Gives a user access to a tenant besides their own (a consultant, say).

    A plain table: the middleware reads it before any RLS context is set.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="unique_membership"),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.tenant}"
