"""Test command: report the RLS context it runs in, as one JSON line.

``orders`` is a raw ``COUNT(*)`` (no ORM auto-scoping), so it counts what the
RLS policy lets through.
"""

from __future__ import annotations

import json
import os
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from django_rls_tenants.tenants.state import get_current_tenant_id


class Command(BaseCommand):
    """Print the tenant ``ContextVar``, the GUCs and the visible ``test_order`` rows."""

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--fail-for", help="Raise CommandError for this tenant pk.")
        parser.add_argument("--exit", type=int, help="Exit with this status.")
        parser.add_argument(
            "--crash-for", help="Kill the process for this tenant pk (only in a worker)."
        )
        parser.add_argument("--warn", help="Write this to stderr.")

    def handle(self, *args, **options):
        database = options["database"]
        tenant = get_current_tenant_id()
        if options["fail_for"] is not None and str(tenant) == options["fail_for"]:
            raise CommandError(f"probe failed for tenant {tenant}")
        if options["crash_for"] is not None and str(tenant) == options["crash_for"]:
            os._exit(1)
        if options["warn"]:
            self.stderr.write(options["warn"])
        with connections[database].cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('rls.current_tenant', true), "
                "current_setting('rls.is_admin', true), (SELECT count(*) FROM test_order)"
            )
            guc, is_admin, orders = cursor.fetchone()
        self.stdout.write(
            json.dumps(
                {
                    "tenant": tenant,
                    "guc": guc,
                    "is_admin": is_admin,
                    "orders": orders,
                    "database": database,
                }
            )
        )
        if options["exit"] is not None:
            sys.exit(options["exit"])
