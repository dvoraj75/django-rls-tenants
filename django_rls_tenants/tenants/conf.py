"""Configuration reader for the ``RLS_TENANTS`` Django setting.

Provides ``RLSTenantsConfig`` which reads and validates
``settings.RLS_TENANTS`` with sensible defaults.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


class RLSTenantsConfig:
    """Read library configuration from ``settings.RLS_TENANTS``.

    All settings live under a single dict::

        RLS_TENANTS = {
            "TENANT_MODEL": "myapp.Tenant",  # Required
            "GUC_PREFIX": "rls",             # Default: "rls"
            "TENANT_FK_FIELD": "tenant",     # Default: "tenant"
            "USER_PARAM_NAME": "as_user",    # Default: "as_user"
            "TENANT_PK_TYPE": "int",         # Default: "int"
            "USE_LOCAL_SET": False,           # Default: False
        }
    """

    @property
    def TENANT_MODEL(self) -> str:
        """Dotted path to the Tenant model (e.g., ``"myapp.Tenant"``)."""
        return self._get("TENANT_MODEL")  # type: ignore[no-any-return]

    @property
    def GUC_PREFIX(self) -> str:
        """Prefix for GUC variable names. Default: ``"rls"``."""
        return self._get("GUC_PREFIX", "rls")  # type: ignore[no-any-return]

    @property
    def GUC_CURRENT_TENANT(self) -> str:
        """Derived: ``"{prefix}.current_tenant"``."""
        return f"{self.GUC_PREFIX}.current_tenant"

    @property
    def GUC_IS_ADMIN(self) -> str:
        """Derived: ``"{prefix}.is_admin"``."""
        return f"{self.GUC_PREFIX}.is_admin"

    @property
    def TENANT_FK_FIELD(self) -> str:
        """FK field name on ``RLSProtectedModel``. Default: ``"tenant"``."""
        return self._get("TENANT_FK_FIELD", "tenant")  # type: ignore[no-any-return]

    @property
    def USER_PARAM_NAME(self) -> str:
        """Parameter name ``@with_rls_context`` looks for. Default: ``"as_user"``."""
        return self._get("USER_PARAM_NAME", "as_user")  # type: ignore[no-any-return]

    @property
    def TENANT_PK_TYPE(self) -> str:
        """SQL cast type for tenant PK. Default: ``"int"``."""
        return self._get("TENANT_PK_TYPE", "int")  # type: ignore[no-any-return]

    @property
    def USE_LOCAL_SET(self) -> bool:
        """Use ``SET LOCAL`` instead of ``set_config``. Default: ``False``."""
        return self._get("USE_LOCAL_SET", default=False)  # type: ignore[no-any-return]

    def _get(self, key: str, default: Any = None) -> Any:
        """Read a key from ``settings.RLS_TENANTS``.

        Raises:
            ValueError: If ``key`` is required (no default) and missing.
        """
        config: dict[str, Any] = getattr(settings, "RLS_TENANTS", {})
        value = config.get(key, default)
        if value is None:
            msg = (
                f"RLS_TENANTS['{key}'] is required. "
                f"Add it to your Django settings: "
                f"RLS_TENANTS = {{'{key}': ...}}"
            )
            raise ValueError(msg)
        return value


rls_tenants_config = RLSTenantsConfig()
