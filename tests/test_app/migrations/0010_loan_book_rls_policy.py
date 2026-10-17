"""Protect the external ``test_external_app.Book`` with AddRLSPolicy, and add ``Loan`` (#112)."""

from __future__ import annotations

import django.db.models.deletion
import django_rls_tenants.operations
import django_rls_tenants.rls.constraints
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0009_assignment"),
        # Creates Book and its tenant foreign key, which the policy reads.
        ("test_external_app", "0001_initial"),
    ]

    operations = [
        django_rls_tenants.operations.AddRLSPolicy(
            model_name="Book",
            field="owner",
            app_label="test_external_app",
        ),
        migrations.CreateModel(
            name="Loan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("borrower", models.CharField(max_length=100)),
                (
                    "book",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="test_external_app.book"
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
                "db_table": "test_loan",
                "abstract": False,
                "constraints": [
                    django_rls_tenants.rls.constraints.RLSConstraint(
                        field="tenant", name="test_app_loan_rls_constraint"
                    )
                ],
            },
        ),
    ]
