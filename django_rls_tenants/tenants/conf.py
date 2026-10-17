"""Configuration reader for the ``RLS_TENANTS`` Django setting.

Provides ``RLSTenantsConfig`` which reads and validates
``settings.RLS_TENANTS`` with sensible defaults.
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from django.conf import settings

from django_rls_tenants.exceptions import RLSConfigurationError

# A TENANT_HEADER value: a header name as a client sends it.
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]+")

# All recognized keys in the RLS_TENANTS configuration dict.
_KNOWN_KEYS = frozenset(
    {
        "DATABASES",
        "EXTERNAL_MODELS",
        "GUC_PREFIX",
        "STRICT_MODE",
        "STRICT_MODE_EXCLUDE",
        "TENANT_FK_FIELD",
        "TENANT_HEADER",
        "TENANT_MODEL",
        "TENANT_PK_TYPE",
        "TENANT_SESSION_KEY",
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
            "EXTERNAL_MODELS": {},                 # Default: {}
            "TENANT_HEADER": "X-Tenant",           # Default: "X-Tenant"
            "TENANT_SESSION_KEY": "rls_tenant",    # Default: "rls_tenant"
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

    @property
    def EXTERNAL_MODELS(self) -> dict[str, str]:
        """Models protected by an ``AddRLSPolicy`` migration. Default: ``{}``.

        Maps a model label (``"app_label.ModelName"``, the model name
        case-insensitive) to the name of its tenant foreign key: a model that
        has no ``RLSConstraint`` (a third-party app's) whose table gets its
        policy from an ``AddRLSPolicy`` operation in a migration of the
        project. The library then treats the model as RLS-protected:
        ``check_rls`` checks its table, foreign keys to it are validated
        against the tenant, the ``select_related()`` and ``prefetch_related()``
        tenant filters apply to joins into it, and check ``W012`` reports its
        unique fields::

            RLS_TENANTS = {
                "EXTERNAL_MODELS": {"library.Book": "tenant"},
            }

        An entry that does not resolve to an installed, non-proxy model with
        that foreign key to ``TENANT_MODEL``, or that names a model with an
        ``RLSConstraint``, is reported by check ``W016``.

        Raises:
            RLSConfigurationError: If the value is not a dict mapping model
                labels to field names.
        """
        value = self._get("EXTERNAL_MODELS", {})
        if not isinstance(value, dict) or not all(
            isinstance(label, str) and isinstance(field, str) for label, field in value.items()
        ):
            msg = (
                f"RLS_TENANTS['EXTERNAL_MODELS'] must be a dict mapping model labels "
                f"('app_label.ModelName') to the name of each model's tenant foreign key, "
                f"got {value!r}. Example: {{'library.Book': 'tenant'}}."
            )
            raise RLSConfigurationError(msg)
        return dict(value)

    @property
    def TENANT_HEADER(self) -> str | None:
        """Request header a ``MultiTenantUser`` selects a tenant with. Default: ``"X-Tenant"``.

        Read by ``RLSTenantMiddleware``, ``RLSTenantAPIViewMixin`` and
        ``RLSTenantModelAdmin`` for a user who is a ``MultiTenantUser`` and
        not a tenant admin; it takes precedence over the session's selection.
        ``None`` turns header selection off.

        Raises:
            RLSConfigurationError: If the value is neither a header name
                (letters, digits and hyphens) nor ``None``.
        """
        header = self._get_name_or_none("TENANT_HEADER", "X-Tenant", example="'X-Tenant'")
        if header is not None and not _HEADER_NAME.fullmatch(header):
            # Django reads "X_Tenant" from no header: it maps HTTP_X_TENANT to "X-Tenant".
            msg = (
                f"RLS_TENANTS['TENANT_HEADER'] must be a header name made of letters, digits "
                f"and hyphens, as clients send it, got {header!r}. Example: 'X-Tenant'."
            )
            raise RLSConfigurationError(msg)
        return header

    @property
    def TENANT_SESSION_KEY(self) -> str | None:
        """Session key holding a ``MultiTenantUser``'s selection. Default: ``"rls_tenant"``.

        Written by ``switch_tenant()`` (and the admin's tenant switcher for
        such a user), cleared by ``clear_tenant_selection()``. ``None`` turns
        session selection off, and with it the admin switcher for these users.

        Raises:
            RLSConfigurationError: If the value is neither a non-empty string
                nor ``None``.
        """
        return self._get_name_or_none("TENANT_SESSION_KEY", "rls_tenant", example="'rls_tenant'")

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
        The set of RLS-protected models, which reads ``EXTERNAL_MODELS``, is
        dropped with it.
        """
        # Lazy import: managers imports this module.
        from django_rls_tenants.tenants.managers import _rls_model_cache  # noqa: PLC0415

        self._config_cache = None
        self._unknown_keys_checked = False
        _rls_model_cache.cache_clear()

    def _get(self, key: str, default: Any = None) -> Any:
        """Read a key from ``settings.RLS_TENANTS``.

        Caches the config dict on first access so repeated property
        reads don't call ``getattr(settings, ...)`` each time. The cache
        is dropped by :meth:`reload`, which fires on ``setting_changed``.
        On first access, warns about any unrecognized keys (likely typos).

        Raises:
            RLSConfigurationError: If ``key`` is required (no default) and missing.
        """
        value = self._config().get(key, default)
        if value is None:
            msg = (
                f"RLS_TENANTS['{key}'] is required. "
                f"Add it to your Django settings: "
                f"RLS_TENANTS = {{'{key}': ...}}"
            )
            raise RLSConfigurationError(msg)
        return value

    def _config(self) -> dict[str, Any]:
        """Return the cached ``RLS_TENANTS`` dict; read (and its keys checked) on first use."""
        cached = self._config_cache
        if cached is None:
            cached = dict(getattr(settings, "RLS_TENANTS", {}))
            self._config_cache = cached
        self._warn_unknown_keys(cached)
        return cached

    def _get_name_or_none(self, key: str, default: str, *, example: str) -> str | None:
        """Read a key whose value is a non-empty string, or ``None`` to turn the feature off.

        Unlike :meth:`_get`, an explicit ``None`` is a value, not a missing key.

        Raises:
            RLSConfigurationError: If the value is neither a non-empty string nor ``None``.
        """
        value = self._config().get(key, default)
        if value is None or (isinstance(value, str) and value.strip()):
            return value
        msg = (
            f"RLS_TENANTS['{key}'] must be a non-empty string or None (to turn it off), "
            f"got {value!r}. Example: {example}."
        )
        raise RLSConfigurationError(msg)

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
