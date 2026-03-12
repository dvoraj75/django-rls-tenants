"""Management command: check_rls.

Verifies that all RLSProtectedModel subclasses have the expected
RLS policies applied in the database. Reports missing or stale policies.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    """Verify RLS policies on all protected tables."""

    help = "Verify that RLS policies exist and are enabled on all RLS-protected tables."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        """Check each RLSProtectedModel subclass."""
        from django.apps import apps  # noqa: PLC0415

        from django_rls_tenants.tenants.models import (  # noqa: PLC0415
            RLSProtectedModel,
        )

        errors: list[str] = []
        checked = 0

        for model in apps.get_models():
            if (
                not issubclass(model, RLSProtectedModel) or model._meta.abstract  # noqa: SLF001
            ):
                continue

            table = model._meta.db_table  # noqa: SLF001
            checked += 1

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                    [table],
                )
                row = cursor.fetchone()
                if row is None:
                    errors.append(f"  {model.__name__} ({table}): table does not exist")
                    continue
                if not row[0]:
                    errors.append(f"  {model.__name__} ({table}): RLS not enabled")
                if not row[1]:
                    errors.append(f"  {model.__name__} ({table}): RLS not forced")

                cursor.execute(
                    "SELECT policyname FROM pg_policies WHERE tablename = %s",
                    [table],
                )
                policies = cursor.fetchall()
                if not policies:
                    errors.append(f"  {model.__name__} ({table}): no RLS policies found")
                else:
                    names = [p[0] for p in policies]
                    self.stdout.write(f"  {model.__name__} ({table}): {', '.join(names)}")

        if errors:
            self.stderr.write(self.style.ERROR(f"\nFound {len(errors)} issue(s):"))
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(f"\nAll {checked} RLS-protected tables verified."))
