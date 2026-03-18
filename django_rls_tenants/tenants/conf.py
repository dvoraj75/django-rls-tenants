"""Configuration reader for the ``RLS_TENANTS`` Django setting.

Provides ``RLSTenantsConfig`` which reads and validates
``settings.RLS_TENANTS`` with sensible defaults.
"""

from __future__ import annotations

import warnings
from typing import Any

from django.conf import settings

from django_rls_tenants.exceptions import RLSConfigurationError

# All recognized keys in the RLS_TENANTS configuration dict.
_KNOWN_KEYS = frozenset(
    {
        "TENANT_MODEL",
        "GUC_PREFIX",
        "TENANT_FK_FIELD",
        "USER_PARAM_NAME",
        "TENANT_PK_TYPE",
        "USE_LOCAL_SET",
    }
)


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

    def __init__(self) -> None:
        self._config_cache: dict[str, Any] | None = None
        self._unknown_keys_checked: bool = False

    def _get(self, key: str, default: Any = None) -> Any:
        """Read a key from ``settings.RLS_TENANTS``.

        Caches the config dict on first access so repeated property
        reads don't call ``getattr(settings, ...)`` each time.
        On first access, warns about any unrecognized keys (likely typos).

        Raises:
            RLSConfigurationError: If ``key`` is required (no default) and missing.
        """
        cached = self._config_cache
        if cached is None:
            cached = dict(getattr(settings, "RLS_TENANTS", {}))
            self._config_cache = cached
        self._warn_unknown_keys(cached)
        value = cached.get(key, default)
        if value is None:
            msg = (
                f"RLS_TENANTS['{key}'] is required. "
                f"Add it to your Django settings: "
                f"RLS_TENANTS = {{'{key}': ...}}"
            )
            raise RLSConfigurationError(msg)
        return value

    def _warn_unknown_keys(self, config: dict[str, Any]) -> None:
        """Emit a warning for any unrecognized keys in ``RLS_TENANTS``.

        Only runs once per instance to avoid repeated warnings.
        """
        if self._unknown_keys_checked:
            return
        self._unknown_keys_checked = True
        unknown = set(config.keys()) - _KNOWN_KEYS
        for key in sorted(unknown):
            warnings.warn(
                f"Unknown key {key!r} in RLS_TENANTS settings. "
                f"Known keys: {', '.join(sorted(_KNOWN_KEYS))}. "
                f"Did you mean one of those?",
                UserWarning,
                stacklevel=4,
            )


rls_tenants_config = RLSTenantsConfig()
