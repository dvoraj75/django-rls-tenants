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
    def rls_tenant_id(self):
        return self.tenant_id

    def __str__(self):
        label = self.email or self.username
        if self.tenant:
            return f"{label} ({self.tenant.name})"
        return f"{label} (admin)"
