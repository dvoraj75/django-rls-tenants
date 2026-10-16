"""Add a model with a one-to-one field for the ``prefetch_related()`` tests (#44)."""

from __future__ import annotations

import django.db.models.deletion
import django_rls_tenants.rls.constraints
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0007_invoice"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderShipment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("carrier", models.CharField(max_length=100)),
                (
                    "order",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shipment",
                        to="test_app.order",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="test_app.tenant"
                    ),
                ),
            ],
            options={
                "db_table": "test_order_shipment",
                "abstract": False,
                "constraints": [
                    django_rls_tenants.rls.constraints.RLSConstraint(
                        field="tenant", name="test_app_ordershipment_rls_constraint"
                    )
                ],
            },
        ),
    ]
