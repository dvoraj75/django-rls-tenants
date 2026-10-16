"""Management command: run_for_all_tenants.

Runs another management command once per tenant, each run inside that
tenant's ``tenant_context()``, and reports each tenant's outcome. Tenants can
be filtered (``--filter``), run in worker processes (``--parallel``), reported
as NDJSON (``--json``), and the run stopped at the first failure
(``--fail-fast``).
"""

from __future__ import annotations

import io
import json
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from multiprocessing import get_context
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import FieldError, ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS

from django_rls_tenants.management import _worker
from django_rls_tenants.management.commands.run_as_tenant import (
    _add_command_arguments,
    _check_alias,
    _inner_args,
    _output_options,
    _tenant_model,
)
from django_rls_tenants.tenants.context import admin_context, tenant_context

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from concurrent.futures import Future

    from django.core.management.base import CommandParser, OutputWrapper

_OK = "ok"
_FAILED = "failed"
_SKIPPED = "skipped"


@dataclass(frozen=True)
class TenantRun:
    """The outcome of the command for one tenant, as reported (one ``--json`` line)."""

    tenant: int | str
    label: str
    status: str
    seconds: float | None = None
    error: str | None = None
    traceback: str | None = None
    stdout: str | None = None
    stderr: str | None = None


def _run_for_tenant(
    command_name: str,
    args: Sequence[str],
    tenant: int | str,
    label: str,
    alias: str,
    *,
    capture: bool,
    streams: dict[str, Any] | None = None,
) -> TenantRun:
    """Run ``command_name`` inside ``tenant_context(tenant)`` and return its outcome.

    Any exception, and a ``SystemExit`` with a non-zero status, is a failure
    of this tenant, not of the whole run. With ``capture=True`` the command's
    output is returned in the result instead of written to ``streams``.
    """
    out: dict[str, Any] = dict(streams or {})
    if capture:
        out["stdout"], out["stderr"] = io.StringIO(), io.StringIO()
    error: str | None = None
    trace: str | None = None
    started = time.monotonic()
    try:
        with tenant_context(tenant, using=alias):
            call_command(command_name, *args, **out)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            error = f"exited with status {exc.code}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
    seconds = round(time.monotonic() - started, 3)
    return TenantRun(
        tenant=tenant,
        label=label,
        status=_OK if error is None else _FAILED,
        seconds=seconds,
        error=error,
        traceback=trace,
        stdout=out["stdout"].getvalue() if capture else None,
        stderr=out["stderr"].getvalue() if capture else None,
    )


def _parse_filters(filters: Sequence[str]) -> dict[str, str]:
    """Turn ``--filter field=value`` options into ``QuerySet.filter()`` keyword arguments."""
    parsed: dict[str, str] = {}
    for item in filters:
        field, sep, value = item.partition("=")
        if not sep or not field:
            msg = f"--filter expects field=value (or a lookup: name__startswith=A), got {item!r}."
            raise CommandError(msg)
        parsed[field] = value
    return parsed


def _list_tenants(filters: dict[str, str], alias: str) -> list[tuple[int | str, str]]:
    """Return ``(pk, label)`` of every tenant that matches ``filters``, in ``pk`` order.

    Read inside ``admin_context()``, like ``run_as_tenant`` resolves a tenant.
    """
    model = _tenant_model()
    try:
        with admin_context(using=alias):
            queryset = model._default_manager.using(alias).filter(**filters)  # noqa: SLF001
            return [(tenant.pk, str(tenant)) for tenant in queryset.order_by("pk")]
    except (FieldError, ValidationError, ValueError) as exc:
        msg = f"Cannot filter {model.__name__} by {filters!r}: {exc}"
        raise CommandError(msg) from exc


class Command(BaseCommand):
    """Run a management command once per tenant, each inside its tenant's context."""

    help = (
        "Run a management command once for every tenant, each run inside that tenant's "
        "tenant_context(), and report each tenant's outcome. Example: "
        "manage.py run_for_all_tenants --parallel 4 --json my_report --since 2026-01-01"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command-line arguments."""
        parser.add_argument(
            "--filter",
            action="append",
            default=[],
            metavar="FIELD=VALUE",
            dest="filters",
            help=(
                "Only run for tenants matching this QuerySet.filter() lookup, for example "
                "is_active=True or slug__startswith=eu-. Repeatable; all must match."
            ),
        )
        parser.add_argument(
            "--parallel",
            type=int,
            default=1,
            metavar="N",
            help="Run up to N tenants at once, in worker processes. Default: 1 (in-process).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print one JSON object per tenant (NDJSON) with the command's captured output.",
        )
        parser.add_argument(
            "--fail-fast",
            action="store_true",
            help="Stop at the first tenant that fails; the tenants not yet run are skipped.",
        )
        _add_command_arguments(parser)

    def handle(self, *args: str, **options: Any) -> None:
        """Run the command for every matching tenant and report the outcome."""
        database: str | None = options["database"]
        alias = database or DEFAULT_DB_ALIAS
        _check_alias(alias)
        parallel: int = options["parallel"]
        if parallel < 1:
            raise CommandError(f"--parallel must be at least 1, got {parallel}.")
        command_name: str = options["command_name"]
        inner = _inner_args(command_name, args, database)
        tenants = _list_tenants(_parse_filters(options["filters"]), alias)
        self._json: bool = options["json"]
        self._verbosity: int = options["verbosity"]
        self._show_traceback: bool = options["traceback"]

        if not tenants:
            if not self._json and self._verbosity:
                self.stdout.write("No tenants matched; nothing to run.")
            return

        runs: list[TenantRun] = []
        if parallel == 1:
            self._run_serially(command_name, inner, tenants, alias, options, runs.append)
        else:
            self._run_in_workers(command_name, inner, tenants, alias, options, runs.append)
        self._summarize(command_name, runs)

    def _run_serially(
        self,
        command_name: str,
        inner: list[str],
        tenants: list[tuple[int | str, str]],
        alias: str,
        options: dict[str, Any],
        record: Callable[[TenantRun], None],
    ) -> None:
        """Run the tenants one after another in this process."""
        streams = _output_options(options)
        stopped = False
        for pk, label in tenants:
            if stopped:
                self._report(TenantRun(tenant=pk, label=label, status=_SKIPPED), record)
                continue
            if not self._json:
                self._write_header(pk, label)
            run = _run_for_tenant(
                command_name, inner, pk, label, alias, capture=self._json, streams=streams
            )
            self._report(run, record)
            stopped = run.status == _FAILED and options["fail_fast"]

    def _run_in_workers(
        self,
        command_name: str,
        inner: list[str],
        tenants: list[tuple[int | str, str]],
        alias: str,
        options: dict[str, Any],
        record: Callable[[TenantRun], None],
    ) -> None:
        """Run the tenants in a pool of spawned worker processes, reporting as they finish."""
        if not settings.SETTINGS_MODULE:
            msg = (
                "--parallel needs a settings module (DJANGO_SETTINGS_MODULE or --settings): "
                "each worker process runs django.setup() itself, and settings configured "
                "with settings.configure() cannot reach it."
            )
            raise CommandError(msg)
        database_names = {name: conf["NAME"] for name, conf in settings.DATABASES.items()}
        workers = min(options["parallel"], len(tenants))
        stopped = False
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=get_context("spawn"),
            initializer=_worker.init_worker,
            initargs=(settings.SETTINGS_MODULE, database_names),
        ) as pool:
            futures: dict[Future[TenantRun], tuple[int | str, str]] = {
                pool.submit(_worker.run_for_tenant, command_name, inner, pk, label, alias): (
                    pk,
                    label,
                )
                for pk, label in tenants
            }
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                pk, label = futures[future]
                try:
                    run = future.result()
                except BrokenProcessPool as exc:
                    run = TenantRun(
                        tenant=pk,
                        label=label,
                        status=_FAILED,
                        error=f"the worker process died: {exc}",
                    )
                self._report(run, record)
                if run.status == _FAILED and options["fail_fast"] and not stopped:
                    stopped = True
                    for pending in futures:
                        pending.cancel()
        for future, (pk, label) in futures.items():
            if future.cancelled():
                self._report(TenantRun(tenant=pk, label=label, status=_SKIPPED), record)

    def _report(self, run: TenantRun, record: Callable[[TenantRun], None]) -> None:
        """Write one tenant's outcome and record it for the summary."""
        record(run)
        if self._json:
            self.stdout.write(json.dumps(asdict(run), default=str))
            return
        if run.stdout is not None:
            # Captured by a worker: the serial run wrote the header and the
            # output as it went.
            self._write_header(run.tenant, run.label)
            self._write_captured(run.stdout, self.stdout)
            self._write_captured(run.stderr, self.stderr)
        where = f"{run.label} (pk={run.tenant})"
        if run.status == _FAILED:
            took = "" if run.seconds is None else f" in {run.seconds}s"
            self.stderr.write(f"{where}: FAILED{took}: {run.error}")
            if self._show_traceback and run.traceback:
                self.stderr.write(run.traceback, ending="")
        elif run.status == _SKIPPED:
            if self._verbosity:
                self.stdout.write(f"{where}: skipped")
        elif self._verbosity:
            self.stdout.write(self.style.SUCCESS(f"{where}: OK in {run.seconds}s"))

    def _write_header(self, tenant: int | str, label: str) -> None:
        """Write the line that introduces a tenant's output."""
        if self._verbosity:
            self.stdout.write(f"== {label} (pk={tenant}) ==")

    @staticmethod
    def _write_captured(text: str | None, stream: OutputWrapper) -> None:
        """Write a worker's captured output as it was written."""
        if text:
            stream.write(text, ending="")

    def _summarize(self, command_name: str, runs: list[TenantRun]) -> None:
        """Print the totals, and fail the command if any tenant failed."""
        failed = [run for run in runs if run.status == _FAILED]
        skipped = sum(run.status == _SKIPPED for run in runs)
        if not self._json and self._verbosity:
            self.stdout.write(
                f"{command_name}: {len(runs) - len(failed) - skipped} tenant(s) succeeded, "
                f"{len(failed)} failed, {skipped} skipped."
            )
        if failed:
            names = ", ".join(f"{run.label} (pk={run.tenant})" for run in failed)
            msg = f"{command_name} failed for {len(failed)} of {len(runs)} tenant(s): {names}"
            raise CommandError(msg)
