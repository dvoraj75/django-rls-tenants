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
