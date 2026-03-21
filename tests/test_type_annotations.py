"""Tests verifying type annotations on public API functions.

Ensures return type annotations are present and correct on:
- RLSManager.get_queryset()
- RLSManager.for_user()
- set_guc()
- with_rls_context decorator

These are pure introspection tests that don't require database access.
"""

from __future__ import annotations

import inspect
import typing
from typing import get_type_hints

from django_rls_tenants.rls.guc import clear_guc, get_guc, set_guc
from django_rls_tenants.tenants.context import with_rls_context
from django_rls_tenants.tenants.managers import RLSManager, TenantQuerySet


class TestGucReturnAnnotations:
    """Verify GUC helper functions have correct return type annotations."""

    def test_set_guc_returns_none(self):
        """set_guc() is annotated with -> None."""
        hints = get_type_hints(set_guc)
        assert hints["return"] is type(None)

    def test_get_guc_return_type(self):
        """get_guc() is annotated with -> str | None."""
        hints = get_type_hints(get_guc)
        ret = hints["return"]
        # str | None is a UnionType in 3.10+
        assert str in typing.get_args(ret)
        assert type(None) in typing.get_args(ret)

    def test_clear_guc_returns_none(self):
        """clear_guc() is annotated with -> None."""
        hints = get_type_hints(clear_guc)
        assert hints["return"] is type(None)


class TestManagerReturnAnnotations:
    """Verify RLSManager methods have correct return type annotations."""

    def test_get_queryset_returns_tenant_queryset(self):
        """RLSManager.get_queryset() is annotated with -> TenantQuerySet."""
        hints = get_type_hints(RLSManager.get_queryset)
        assert hints["return"] is TenantQuerySet

    def test_for_user_has_return_annotation(self):
        """RLSManager.for_user() has a return type annotation."""
        # get_type_hints() can't resolve TYPE_CHECKING-only imports (TenantUser),
        # so we inspect the raw annotation string instead.
        sig = inspect.signature(RLSManager.for_user)
        assert sig.return_annotation is not inspect.Parameter.empty
        # The raw annotation (stringified by `from __future__ import annotations`)
        # should reference TenantQuerySet.
        ann = RLSManager.for_user.__annotations__
        assert "return" in ann

    def test_queryset_for_user_has_return_annotation(self):
        """TenantQuerySet.for_user() has a return type annotation."""
        ann = TenantQuerySet.for_user.__annotations__
        assert "return" in ann


class TestWithRlsContextAnnotations:
    """Verify with_rls_context decorator has proper type annotations."""

    def test_decorator_preserves_function_name(self):
        """@with_rls_context preserves __name__ via functools.wraps."""

        @with_rls_context
        def my_service_function(as_user):
            return "result"

        assert my_service_function.__name__ == "my_service_function"

    def test_decorator_preserves_docstring(self):
        """@with_rls_context preserves __doc__ via functools.wraps."""

        @with_rls_context
        def my_service_function(as_user):
            """My docstring."""
            return "result"

        assert my_service_function.__doc__ == "My docstring."

    def test_decorator_with_user_param_preserves_name(self):
        """@with_rls_context(user_param=...) preserves __name__."""

        @with_rls_context(user_param="current_user")
        def my_service_function(current_user):
            return "result"

        assert my_service_function.__name__ == "my_service_function"

    def test_has_return_annotation(self):
        """with_rls_context() has a return type annotation."""
        ann = with_rls_context.__annotations__
        assert "return" in ann

    def test_func_param_has_annotation(self):
        """with_rls_context() 'func' parameter has a type annotation."""
        ann = with_rls_context.__annotations__
        assert "func" in ann

    def test_user_param_has_annotation(self):
        """with_rls_context() 'user_param' parameter has a type annotation."""
        ann = with_rls_context.__annotations__
        assert "user_param" in ann

    def test_signature_uses_paramspec(self):
        """with_rls_context uses ParamSpec (_P) and TypeVar (_R) in its signature."""
        # Verify the raw annotation strings reference the ParamSpec/TypeVar
        ann = with_rls_context.__annotations__
        func_ann = ann["func"]
        return_ann = ann["return"]
        # The stringified annotations should reference _P and _R
        assert "_P" in str(func_ann)
        assert "_R" in str(func_ann)
        assert "_P" in str(return_ann)
        assert "_R" in str(return_ann)

    def test_decorated_function_is_callable(self):
        """Decorated function remains callable (basic smoke test, no DB)."""

        @with_rls_context
        def compute(as_user, x: int, y: int) -> int:
            return x + y

        # Verify it's callable (don't invoke -- that needs DB for GUC)
        assert callable(compute)
        assert compute.__wrapped__  # functools.wraps sets __wrapped__

    def test_decorator_factory_returns_callable(self):
        """with_rls_context(user_param=...) returns a decorator."""
        decorator = with_rls_context(user_param="current_user")
        assert callable(decorator)

        @decorator
        def my_func(current_user):
            return "result"

        assert my_func.__name__ == "my_func"
        assert callable(my_func)
