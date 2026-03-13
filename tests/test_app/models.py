"""Test models exercising all django-rls-tenants features."""

from __future__ import annotations

from django.db import models

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.tenants.models import RLSProtectedModel


class Tenant(models.Model):
    """Not RLS-protected -- the tenant table itself is global."""

    name = models.CharField(max_length=100)

    class Meta:
        db_table = "test_tenant"

    def __str__(self) -> str:
        return self.name


class TenantUser(models.Model):
    """Test user implementing the ``TenantUser`` protocol."""

    username = models.CharField(max_length=100)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    is_admin = models.BooleanField(default=False)

    class Meta:
        db_table = "test_user"

    def __str__(self) -> str:
        return self.username

    @property
    def is_tenant_admin(self) -> bool:
        """Return ``True`` if this user should bypass RLS."""
        return self.is_admin

    @property
    def rls_tenant_id(self) -> int | None:
        """Return the tenant ID for RLS filtering."""
        return self.tenant_id  # type: ignore[return-value]

    @property
    def is_authenticated(self) -> bool:
        """Always authenticated (test user)."""
        return True


class Order(RLSProtectedModel):
    """Standard RLS-protected model for testing."""

    product = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_order"


class Document(RLSProtectedModel):
    """Second RLS-protected model for cross-model isolation tests."""

    title = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_document"


class ProtectedUser(RLSProtectedModel):
    """RLS-protected user model with extra bypass flags."""

    email = models.CharField(max_length=255)
    tenant = models.ForeignKey(
        "test_app.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_protected_user"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            RLSConstraint(
                field="tenant",
                name="test_app_protecteduser_rls_constraint",
                extra_bypass_flags=[
                    "rls.is_login_request",
                    "rls.is_preauth_request",
                ],
            ),
        ]
