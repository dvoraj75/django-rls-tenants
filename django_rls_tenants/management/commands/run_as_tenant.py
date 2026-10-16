"""Management command: run_as_tenant.

Runs another management command inside one tenant's RLS context
(``tenant_context()``), or inside ``admin_context()`` with ``--admin``, so a
command written without any context handling (a report, a backfill, a data
fix) sees exactly one tenant's rows.

The helpers here are shared with ``run_for_all_tenants``.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.core.exceptions import (
    FieldError,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    ValidationError,
)
from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections
from django.utils.connection import ConnectionDoesNotExist

from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import admin_context, tenant_context

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from django.core.management.base import CommandParser
    from django.db import models


def _add_command_arguments(parser: CommandParser) -> None:
    """Add ``--database``, the inner command's name and its arguments to ``parser``."""
    parser.add_argument(
        "--database",
        help=(
            "Database alias to enter the RLS context on and to read tenants from; also "
            "passed on to the command when it has a --database option. Default: 'default'."
        ),
    )
    parser.add_argument("command_name", metavar="command", help="The management command to run.")
    parser.add_argument(
        "args",
        metavar="...",
        nargs=argparse.REMAINDER,
        help=(
            "Arguments and options for the command. Everything after the command's name "
            "is passed on to it, so put this command's own options before the name."
        ),
    )


def _check_alias(alias: str) -> None:
    """Raise ``CommandError`` unless ``alias`` can carry an RLS context for a whole command.

    With ``USE_LOCAL_SET=True`` a context entered on an alias outside
    ``RLS_TENANTS["DATABASES"]`` needs ``transaction.atomic()``; these
    commands do not wrap the inner command in a transaction of theirs.
    """
    try:
        connections[alias]
    except ConnectionDoesNotExist as exc:
        raise CommandError(f"Unknown database alias {alias!r}: {exc}") from exc
    if rls_tenants_config.USE_LOCAL_SET and alias not in rls_tenants_config.DATABASES:
        msg = (
            f"Database alias {alias!r} is not in RLS_TENANTS['DATABASES'], so with "
            f"USE_LOCAL_SET=True a context entered on it needs transaction.atomic() around "
            f"the whole command. Add {alias!r} to RLS_TENANTS['DATABASES']."
        )
        raise CommandError(msg)


def _inner_args(command_name: str, args: Sequence[str], database: str | None) -> list[str]:
    """Return the argument list for ``command_name``, with ``--database`` passed on.

    Raises ``CommandError`` for an unknown command, before any tenant is
    resolved. ``--database`` goes first, so a ``--database`` the caller wrote
    after the command's name wins, and only to a command that declares it
    (argparse would reject it otherwise).
    """
    try:
        app_name = get_commands()[command_name]
    except KeyError as exc:
        raise CommandError(f"Unknown command: {command_name!r}") from exc
    command = load_command_class(app_name, command_name)
    inner = list(args)
    if inner[:1] == ["--"]:
        inner = inner[1:]
    if database is None:
        return inner
    parser = command.create_parser("", command_name)
    # argparse has no public accessor for the declared option strings.
    if "--database" in parser._option_string_actions:  # noqa: SLF001
        inner.insert(0, f"--database={database}")
    return inner


def _tenant_model() -> type[models.Model]:
    """Return the ``RLS_TENANTS["TENANT_MODEL"]`` class."""
    return apps.get_model(rls_tenants_config.TENANT_MODEL)


def _resolve_tenant(value: str, lookup: str, alias: str) -> models.Model:
    """Return the tenant whose ``lookup`` field equals ``value`` on ``alias``.

    Read inside ``admin_context()``, so a tenant model that is itself
    RLS-protected still resolves.
    """
    model = _tenant_model()
    try:
        with admin_context(using=alias):
            return model._default_manager.using(alias).get(**{lookup: value})  # noqa: SLF001
    except ObjectDoesNotExist as exc:
        msg = f"No {model.__name__} with {lookup}={value!r} on database {alias!r}."
        raise CommandError(msg) from exc
    except MultipleObjectsReturned as exc:
        msg = (
            f"More than one {model.__name__} has {lookup}={value!r}. Pass --lookup with a "
            f"unique field."
        )
        raise CommandError(msg) from exc
    except (FieldError, ValidationError, ValueError) as exc:
        msg = f"Cannot look up a {model.__name__} by {lookup}={value!r}: {exc}"
        raise CommandError(msg) from exc


def _output_options(options: dict[str, Any]) -> dict[str, Any]:
    """Return the ``call_command()`` keyword arguments that keep this command's output setup.

    The raw streams (not this command's ``OutputWrapper``, which would style
    the inner command's already styled output again) and the color flags.
    """
    passed: dict[str, Any] = {
        "stdout": options.get("stdout") or sys.stdout,
        "stderr": options.get("stderr") or sys.stderr,
    }
    for flag in ("no_color", "force_color"):
        if options.get(flag):
            passed[flag] = True
    return passed


class Command(BaseCommand):
    """Run a management command inside one tenant's RLS context."""

    help = (
        "Run a management command inside one tenant's RLS context (tenant_context()), or "
        "inside admin_context() with --admin. Example: "
        "manage.py run_as_tenant --tenant acme --lookup slug my_report --since 2026-01-01"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command-line arguments."""
        # Not required=True: call_command() would put a required option it
        # receives as a keyword after the inner command's arguments, where the
        # REMAINDER positional swallows it. handle() checks it instead.
        target = parser.add_mutually_exclusive_group()
        target.add_argument(
            "--tenant",
            help="The tenant to run as: its primary key, or the value of its --lookup field.",
        )
        target.add_argument(
            "--admin",
            action="store_true",
            help="Run inside admin_context() instead: the command sees every tenant's rows.",
        )
        parser.add_argument(
            "--lookup",
            default="pk",
            help="Tenant model field that --tenant is matched against (e.g. slug). Default: pk.",
        )
        _add_command_arguments(parser)

    def handle(self, *args: str, **options: Any) -> None:
        """Resolve the tenant and run the command inside its context."""
        database: str | None = options["database"]
        alias = database or DEFAULT_DB_ALIAS
        _check_alias(alias)
        command_name: str = options["command_name"]
        inner = _inner_args(command_name, args, database)

        context: AbstractContextManager[None]
        if options["admin"]:
            context = admin_context(using=alias)
            target = "the admin context"
        elif options["tenant"] is not None:
            tenant = _resolve_tenant(options["tenant"], options["lookup"], alias)
            context = tenant_context(tenant.pk, using=alias)
            target = f"tenant {tenant} (pk={tenant.pk})"
        else:
            msg = "Pass --tenant <value> to run as one tenant, or --admin for the admin context."
            raise CommandError(msg)

        if options["verbosity"] >= 2:  # noqa: PLR2004  -- Django's verbose level
            self.stderr.write(f"Running {command_name} in {target} on database {alias!r}.")
        with context:
            call_command(command_name, *inner, **_output_options(options))
