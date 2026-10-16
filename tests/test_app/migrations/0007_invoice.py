"""Add a model with a per-tenant unique constraint for the DRF serializer tests."""

from __future__ import annotations

import django.db.models.deletion
import django_rls_tenants.rls.constraints
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0006_nested_nullable_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="Invoice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("number", models.CharField(max_length=50)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="test_app.tenant"
                    ),
                ),
            ],
            options={
                "db_table": "test_invoice",
                "abstract": False,
                "constraints": [
                    django_rls_tenants.rls.constraints.RLSConstraint(
                        field="tenant", name="test_app_invoice_rls_constraint"
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "number"), name="test_invoice_tenant_number_uniq"
                    ),
                ],
            },
        ),
    ]
