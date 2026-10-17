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
        "DATABASES",
        "GUC_PREFIX",
        "STRICT_MODE",
        "STRICT_MODE_EXCLUDE",
        "TENANT_FK_FIELD",
        "TENANT_MODEL",
        "TENANT_PK_TYPE",
        "UNIQUE_ACROSS_TENANTS",
        "USE_LOCAL_SET",
        "USER_PARAM_NAME",
    }
)


class RLSTenantsConfig:
    """Read library configuration from ``settings.RLS_TENANTS``.

    All settings live under a single dict::

        RLS_TENANTS = {
            "TENANT_MODEL": "myapp.Tenant",       # Required
            "DATABASES": ["default"],              # Default: ["default"]
            "GUC_PREFIX": "rls",                   # Default: "rls"
            "STRICT_MODE": False,                  # Default: False
            "STRICT_MODE_EXCLUDE": [],             # Default: []
            "TENANT_FK_FIELD": "tenant",           # Default: "tenant"
            "USER_PARAM_NAME": "as_user",          # Default: "as_user"
            "TENANT_PK_TYPE": "int",               # Default: "int"
            "USE_LOCAL_SET": False,                 # Default: False
            "UNIQUE_ACROSS_TENANTS": {},           # Default: {}
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
        """Set the GUCs transaction-locally (``SET LOCAL``). Default: ``False``.

        The GUC affinity guard then sets the active context's values in each
        transaction, giving a statement run in autocommit inside a context a
        transaction of its own.
        """
        return self._get("USE_LOCAL_SET", default=False)  # type: ignore[no-any-return]

    @property
    def STRICT_MODE(self) -> bool:
        """Raise on queries without tenant context. Default: ``False``.

        When enabled, ``TenantQuerySet`` evaluation methods raise
        ``NoTenantContextError`` if no RLS context is active (no
        ``tenant_context()``, ``admin_context()``, ``for_user()``,
        or ``RLSTenantMiddleware``).
        """
        return self._get("STRICT_MODE", default=False)  # type: ignore[no-any-return]

    @property
    def STRICT_MODE_EXCLUDE(self) -> tuple[str, ...]:
        """Models ``STRICT_MODE`` does not guard. Default: ``()``.

        A list of model labels (``"app_label.ModelName"``, the model name
        case-insensitive). A query on a listed model, or on a proxy of one,
        runs without an active RLS context instead of raising
        ``NoTenantContextError``; the auto-scope filter, ``for_user()`` and
        the RLS policies are unchanged, so with no context it reads only the
        rows the policy lets through (none, unless a bypass flag is set)::

            RLS_TENANTS = {
                "STRICT_MODE": True,
                "STRICT_MODE_EXCLUDE": ["accounts.User"],
            }

        A label that names no installed RLS-protected model is reported by
        check ``W015``.

        Raises:
            RLSConfigurationError: If the value is not a list of strings.
        """
        value = self._get("STRICT_MODE_EXCLUDE", ())
        if not isinstance(value, (list, tuple, set, frozenset)) or not all(
            isinstance(label, str) for label in value
        ):
            msg = (
                f"RLS_TENANTS['STRICT_MODE_EXCLUDE'] must be a list of model labels "
                f"('app_label.ModelName'), got {value!r}. Example: ['accounts.User']."
            )
            raise RLSConfigurationError(msg)
        return tuple(value)

    @property
    def DATABASES(self) -> list[str]:
        """Database aliases that follow the active RLS context. Default: ``["default"]``.

        The middleware sets the GUCs on each of them (with
        ``USE_LOCAL_SET=True`` on its synchronous path only), and the GUC
        affinity guard applies the active context on each of them before
        every statement, whichever alias the context was entered on
        (transaction-locally with ``USE_LOCAL_SET=True``). With
        ``USE_LOCAL_SET=True`` an alias outside this list still needs
        ``transaction.atomic()`` around a context entered on it.
        In multi-database setups (e.g., read replicas), add all aliases
        that serve RLS-protected queries::

            RLS_TENANTS = {
                "DATABASES": ["default", "replica"],
            }
        """
        return self._get("DATABASES", ["default"])  # type: ignore[no-any-return]

    @property
    def UNIQUE_ACROSS_TENANTS(self) -> dict[str, list[str | tuple[str, ...]]]:
        """Unique fields and constraints meant to span tenants. Default: ``{}``.

        Maps a model label (``"app_label.ModelName"``) to the unique sets of
        that model that check ``W012`` must not report: a field name (a
        ``unique=True`` field, or a unique set of that one field), a
        ``UniqueConstraint`` name, or a tuple of field names (a
        ``unique_together`` entry or a ``UniqueConstraint(fields=...)`` with
        exactly those fields)::

            RLS_TENANTS = {
                "UNIQUE_ACROSS_TENANTS": {
                    "billing.Company": ["registration_number", ("country", "vat_id")],
                },
            }

        Raises:
            RLSConfigurationError: If the value is not a dict of lists of field
                names, constraint names and tuples of field names.
        """
        value = self._get("UNIQUE_ACROSS_TENANTS", {})
        if not isinstance(value, dict):
            msg = (
                f"RLS_TENANTS['UNIQUE_ACROSS_TENANTS'] must be a dict mapping model "
                f"labels to lists, got {type(value).__name__}. Example: "
                f"{{'billing.Company': ['registration_number']}}."
            )
            raise RLSConfigurationError(msg)
        allowlist: dict[str, list[str | tuple[str, ...]]] = {}
        for label, entries in value.items():
            if not isinstance(label, str) or not isinstance(entries, (list, tuple)):
                msg = (
                    f"RLS_TENANTS['UNIQUE_ACROSS_TENANTS'] maps {label!r} to {entries!r}: "
                    f"each key must be a model label ('app_label.ModelName') and each "
                    f"value a list of field names, constraint names and tuples of field "
                    f"names, e.g. {{'billing.Company': ['registration_number']}}."
                )
                raise RLSConfigurationError(msg)
            allowlist[label] = [_allowlist_entry(label, entry) for entry in entries]
        return allowlist

    def __repr__(self) -> str:
        try:
            model = self.TENANT_MODEL
        except Exception:
            model = "<unset>"
        return f"RLSTenantsConfig(TENANT_MODEL={model!r})"

    def __init__(self) -> None:
        self._config_cache: dict[str, Any] | None = None
        self._unknown_keys_checked: bool = False

    def reload(self) -> None:
        """Discard the cached ``RLS_TENANTS`` dict so the next read hits ``settings``.

        The app config connects this to Django's ``setting_changed`` signal, so
        ``override_settings(RLS_TENANTS=...)``, ``@override_settings`` on a test
        class and pytest-django's ``settings`` fixture all take effect without
        touching the cache by hand. The unknown-key check runs again on the
        next read as well, so a typo introduced by an override is still reported.
        """
        self._config_cache = None
        self._unknown_keys_checked = False

    def _get(self, key: str, default: Any = None) -> Any:
        """Read a key from ``settings.RLS_TENANTS``.

        Caches the config dict on first access so repeated property
        reads don't call ``getattr(settings, ...)`` each time. The cache
        is dropped by :meth:`reload`, which fires on ``setting_changed``.
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


def _allowlist_entry(label: str, entry: Any) -> str | tuple[str, ...]:
    """Validate one ``UNIQUE_ACROSS_TENANTS`` entry; a list of names becomes a tuple.

    Raises:
        RLSConfigurationError: If ``entry`` is neither a name nor a sequence of names.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, (list, tuple)) and entry and all(isinstance(name, str) for name in entry):
        return tuple(entry)
    msg = (
        f"RLS_TENANTS['UNIQUE_ACROSS_TENANTS'][{label!r}] contains {entry!r}: an entry "
        f"must be a field or constraint name (a string) or a non-empty tuple of field "
        f"names, e.g. ('country', 'vat_id')."
    )
    raise RLSConfigurationError(msg)


rls_tenants_config = RLSTenantsConfig()
