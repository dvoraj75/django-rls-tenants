"""Add ``Label``, protected by its ``RLSConstraint`` alone, and ``Board``, whose M2M points at it (#159)."""

from __future__ import annotations

import django.db.models.deletion
import django_rls_tenants.rls.constraints
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0012_membership"),
    ]

    operations = [
        migrations.CreateModel(
            name="Label",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                ("tags", models.ManyToManyField(to="test_app.tag")),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="test_app.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "test_label",
            },
        ),
        migrations.CreateModel(
            name="Board",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="test_app.tenant",
                    ),
                ),
                ("labels", models.ManyToManyField(to="test_app.label")),
            ],
            options={
                "db_table": "test_board",
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="label",
            constraint=django_rls_tenants.rls.constraints.RLSConstraint(
                field="tenant", name="test_app_label_rls_constraint"
            ),
        ),
        migrations.AddConstraint(
            model_name="board",
            constraint=django_rls_tenants.rls.constraints.RLSConstraint(
                field="tenant", name="test_app_board_rls_constraint"
            ),
        ),
    ]
