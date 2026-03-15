"""Tests for django_rls_tenants.tenants.models."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.db import models

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.tenants.managers import RLSManager
from django_rls_tenants.tenants.models import RLSProtectedModel, _add_tenant_fk
from tests.test_app.models import Document, Order, ProtectedUser, Tenant

pytestmark = pytest.mark.django_db


class TestDynamicTenantFK:
    """Tests for the class_prepared signal that adds tenant FK."""

    def test_order_gets_tenant_fk(self):
        """Order (standard RLSProtectedModel) has an auto-generated tenant FK."""
        field_names = [f.name for f in Order._meta.local_fields]
        assert "tenant" in field_names

    def test_document_gets_tenant_fk(self):
        """Document (standard RLSProtectedModel) has an auto-generated tenant FK."""
        field_names = [f.name for f in Document._meta.local_fields]
        assert "tenant" in field_names

    def test_tenant_fk_target(self):
        """Auto-generated FK points to the configured TENANT_MODEL."""
        tenant_field = Order._meta.get_field("tenant")
        assert tenant_field.related_model is Tenant

    def test_tenant_fk_not_nullable(self):
        """Auto-generated FK is NOT NULL (required)."""
        tenant_field = Order._meta.get_field("tenant")
        assert tenant_field.null is False

    def test_explicit_fk_not_overridden(self):
        """ProtectedUser defines its own tenant FK -- signal skips it.

        The explicit FK is nullable (unlike the auto-generated one).
        """
        tenant_field = ProtectedUser._meta.get_field("tenant")
        assert tenant_field.null is True
        assert tenant_field.blank is True

    def test_non_rls_model_unaffected(self):
        """Non-RLSProtectedModel models are not modified by the signal."""
        field_names = [f.name for f in Tenant._meta.local_fields]
        # Tenant has "id" and "name", no "tenant" FK
        assert "tenant" not in field_names


class TestRLSProtectedModelMeta:
    """Tests for RLSProtectedModel Meta and manager."""

    def test_meta_constraints_inherited(self):
        """Subclass inherits RLSConstraint from RLSProtectedModel.Meta."""
        constraints = Order._meta.constraints
        rls_constraints = [c for c in constraints if isinstance(c, RLSConstraint)]
        assert len(rls_constraints) == 1
        assert rls_constraints[0].field == "tenant"

    def test_meta_constraints_overridden(self):
        """ProtectedUser overrides constraints with custom RLSConstraint."""
        constraints = ProtectedUser._meta.constraints
        rls_constraints = [c for c in constraints if isinstance(c, RLSConstraint)]
        assert len(rls_constraints) == 1
        assert rls_constraints[0].extra_bypass_flags == [
            "rls.is_login_request",
            "rls.is_preauth_request",
        ]

    def test_default_manager_is_rls_manager(self):
        """RLSProtectedModel subclasses use RLSManager as the default manager."""
        assert isinstance(Order.objects, RLSManager)

    def test_abstract_model(self):
        """RLSProtectedModel itself is abstract."""
        assert RLSProtectedModel._meta.abstract is True


class TestAddTenantFKConfig:
    """Tests for _add_tenant_fk respecting TENANT_FK_FIELD configuration."""

    def test_respects_custom_tenant_fk_field(self):
        """_add_tenant_fk uses the configured TENANT_FK_FIELD, not hardcoded 'tenant'.

        When TENANT_FK_FIELD is set to a custom name, the signal handler should
        check for that name when deciding whether to add the FK.
        """
        with patch("django_rls_tenants.tenants.conf.rls_tenants_config") as mock_config:
            mock_config.TENANT_FK_FIELD = "organization"
            mock_config.TENANT_MODEL = "test_app.Tenant"

            # Order already has a field named "tenant" but NOT "organization".
            # With the old hardcoded check, it would skip (because "tenant" IS in
            # local_field_names). With the fix, it should try to add "organization".
            # We patch contribute_to_class to verify it's called with the right name.
            with patch.object(models.ForeignKey, "contribute_to_class") as mock_ctc:
                _add_tenant_fk(sender=Order)
                mock_ctc.assert_called_once()
                _, call_field_name = mock_ctc.call_args[0]
                assert call_field_name == "organization"

    def test_no_duplicate_when_model_defines_configured_fk(self):
        """No FK is added when the model already defines a field matching TENANT_FK_FIELD.

        If a user configures TENANT_FK_FIELD='tenant' and their model already
        has a 'tenant' field (like ProtectedUser), the signal must skip it.
        """
        with patch("django_rls_tenants.tenants.conf.rls_tenants_config") as mock_config:
            mock_config.TENANT_FK_FIELD = "tenant"
            mock_config.TENANT_MODEL = "test_app.Tenant"

            with patch.object(models.ForeignKey, "contribute_to_class") as mock_ctc:
                # ProtectedUser already defines "tenant" field
                _add_tenant_fk(sender=ProtectedUser)
                mock_ctc.assert_not_called()
