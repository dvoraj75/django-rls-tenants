"""Worker process entry points for ``run_for_all_tenants --parallel``.

The pool starts its workers with ``spawn``: a forked child would share the
parent's PostgreSQL sockets. A spawned worker imports this module before it
has set Django up, so it must not import the tenants layer (which imports
models) at module level; ``run_for_tenant()`` imports the command module once
``init_worker()`` has run ``django.setup()``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from django_rls_tenants.management.commands.run_for_all_tenants import TenantRun


def init_worker(settings_module: str, database_names: Mapping[str, str]) -> None:
    """Set Django up in a worker, on the databases the parent process uses.

    Args:
        settings_module: The parent's ``settings.SETTINGS_MODULE``.
        database_names: ``NAME`` of each alias in the parent's
            ``settings.DATABASES``. They differ from what the settings module
            computes when the parent changed them at runtime (a test database).
    """
    import django  # noqa: PLC0415  -- see module docstring
    from django.conf import settings  # noqa: PLC0415

    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    for alias, name in database_names.items():
        if alias in settings.DATABASES:
            settings.DATABASES[alias]["NAME"] = name
    django.setup()


def run_for_tenant(
    command_name: str,
    args: Sequence[str],
    tenant: int | str,
    label: str,
    alias: str,
) -> TenantRun:
    """Run the command for one tenant, capturing its output, and return the result."""
    from django_rls_tenants.management.commands.run_for_all_tenants import (  # noqa: PLC0415
        _run_for_tenant,
    )

    return _run_for_tenant(command_name, args, tenant, label, alias, capture=True)
