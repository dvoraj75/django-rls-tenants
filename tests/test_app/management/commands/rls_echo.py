"""Test command without a ``--database`` option: print its arguments as JSON."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from django_rls_tenants.tenants.state import get_current_tenant_id


class Command(BaseCommand):
    """Print the positional arguments, ``--flag`` and the current tenant."""

    def add_arguments(self, parser):
        parser.add_argument("words", nargs="*")
        parser.add_argument("--flag")

    def handle(self, *args, **options):
        tenant = get_current_tenant_id()
        payload = {"words": options["words"], "flag": options["flag"], "tenant": tenant}
        self.stdout.write(json.dumps(payload))
