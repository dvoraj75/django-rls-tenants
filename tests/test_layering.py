"""Layering enforcement and package structure tests.

Verifies that the ``rls/`` layer has zero imports from ``tenants/``,
and that the top-level ``__init__.py`` lazy imports resolve correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_rls_has_no_tenant_imports():
    """Scan all rls/ source files for imports from tenants/."""
    rls_dir = Path(__file__).resolve().parent.parent / "django_rls_tenants" / "rls"
    violations = []

    for py_file in rls_dir.rglob("*.py"):
        content = py_file.read_text()
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            # Skip comments and empty lines
            if stripped.startswith("#") or not stripped:
                continue
            # Check for imports from tenants
            if (
                "from django_rls_tenants.tenants" in stripped
                or "import django_rls_tenants.tenants" in stripped
            ):
                rel_path = py_file.relative_to(rls_dir.parent.parent)
                violations.append(f"  {rel_path}:{i}: {stripped}")

    assert not violations, (
        f"rls/ layer must not import from tenants/. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


class TestLazyImports:
    """Verify top-level lazy imports from ``django_rls_tenants``."""

    def test_rls_constraint(self):
        """RLSConstraint is importable from the top-level package."""
        from django_rls_tenants import RLSConstraint  # noqa: PLC0415
        from django_rls_tenants.rls.constraints import RLSConstraint as DirectCls  # noqa: PLC0415

        assert RLSConstraint is DirectCls

    def test_rls_protected_model(self):
        """RLSProtectedModel is importable from the top-level package."""
        from django_rls_tenants import RLSProtectedModel  # noqa: PLC0415
        from django_rls_tenants.tenants import models as models_mod  # noqa: PLC0415

        assert RLSProtectedModel is models_mod.RLSProtectedModel

    def test_context_managers(self):
        """Context managers are importable from the top-level package."""
        from django_rls_tenants import admin_context, tenant_context  # noqa: PLC0415
        from django_rls_tenants.tenants import context as ctx_mod  # noqa: PLC0415

        assert admin_context is ctx_mod.admin_context
        assert tenant_context is ctx_mod.tenant_context

    def test_invalid_attribute_raises(self):
        """Accessing an undefined attribute raises AttributeError."""
        import django_rls_tenants  # noqa: PLC0415

        with pytest.raises(AttributeError, match="no attribute"):
            _ = django_rls_tenants.nonexistent_symbol


class TestRemovedTopLevelExports:
    """Verify internal helpers are NOT re-exported from the top-level package.

    These symbols were removed from ``__all__`` and ``_LAZY_IMPORTS`` in v1.2.1
    to guide users toward the safe context manager APIs instead of raw state
    manipulation.  They remain importable from their actual submodules.
    """

    # -- State functions: NOT in top-level --

    @pytest.mark.parametrize(
        "name",
        [
            "get_current_tenant_id",
            "set_current_tenant_id",
            "reset_current_tenant_id",
            "get_rls_context_active",
            "set_rls_context_active",
            "reset_rls_context_active",
        ],
    )
    def test_state_functions_not_in_top_level(self, name):
        """Raw state functions are not importable from the top-level package."""
        import django_rls_tenants  # noqa: PLC0415

        assert name not in django_rls_tenants.__all__
        with pytest.raises(AttributeError, match="no attribute"):
            getattr(django_rls_tenants, name)

    # -- Exception classes: NOT in top-level --

    @pytest.mark.parametrize(
        "name",
        [
            "NoTenantContextError",
            "RLSConfigurationError",
            "RLSTenantError",
        ],
    )
    def test_exceptions_not_in_top_level(self, name):
        """Exception classes are not importable from the top-level package."""
        import django_rls_tenants  # noqa: PLC0415

        assert name not in django_rls_tenants.__all__
        with pytest.raises(AttributeError, match="no attribute"):
            getattr(django_rls_tenants, name)

    # -- State functions: still importable from submodule --

    def test_get_current_tenant_id_from_submodule(self):
        """get_current_tenant_id is importable from its actual module."""
        from django_rls_tenants.tenants.state import get_current_tenant_id  # noqa: PLC0415

        assert callable(get_current_tenant_id)

    def test_set_current_tenant_id_from_submodule(self):
        """set_current_tenant_id is importable from its actual module."""
        from django_rls_tenants.tenants.state import set_current_tenant_id  # noqa: PLC0415

        assert callable(set_current_tenant_id)

    def test_reset_current_tenant_id_from_submodule(self):
        """reset_current_tenant_id is importable from its actual module."""
        from django_rls_tenants.tenants.state import reset_current_tenant_id  # noqa: PLC0415

        assert callable(reset_current_tenant_id)

    def test_get_rls_context_active_from_submodule(self):
        """get_rls_context_active is importable from its actual module."""
        from django_rls_tenants.tenants.state import get_rls_context_active  # noqa: PLC0415

        assert callable(get_rls_context_active)

    def test_set_rls_context_active_from_submodule(self):
        """set_rls_context_active is importable from its actual module."""
        from django_rls_tenants.tenants.state import set_rls_context_active  # noqa: PLC0415

        assert callable(set_rls_context_active)

    def test_reset_rls_context_active_from_submodule(self):
        """reset_rls_context_active is importable from its actual module."""
        from django_rls_tenants.tenants.state import reset_rls_context_active  # noqa: PLC0415

        assert callable(reset_rls_context_active)

    # -- Exception classes: still importable from submodule --

    def test_no_tenant_context_error_from_submodule(self):
        """NoTenantContextError is importable from its actual module."""
        from django_rls_tenants.exceptions import NoTenantContextError  # noqa: PLC0415

        assert issubclass(NoTenantContextError, Exception)

    def test_rls_configuration_error_from_submodule(self):
        """RLSConfigurationError is importable from its actual module."""
        from django_rls_tenants.exceptions import RLSConfigurationError  # noqa: PLC0415

        assert issubclass(RLSConfigurationError, Exception)

    def test_rls_tenant_error_from_submodule(self):
        """RLSTenantError is importable from its actual module."""
        from django_rls_tenants.exceptions import RLSTenantError  # noqa: PLC0415

        assert issubclass(RLSTenantError, Exception)
