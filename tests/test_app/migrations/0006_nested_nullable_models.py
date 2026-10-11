"""Add model chains for nested nullable select_related regression tests."""

from __future__ import annotations

import django.db.models.deletion
import django_rls_tenants.rls.constraints
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0005_m2m_rls_policies"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoteRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("summary", models.CharField(max_length=255)),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="test_app.orderitem"
                    ),
                ),
                (
                    "note",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="test_app.ordernote"
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
                "db_table": "test_note_revision",
                "abstract": False,
                "constraints": [
                    django_rls_tenants.rls.constraints.RLSConstraint(
                        field="tenant", name="test_app_noterevision_rls_constraint"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="OrderNoteAttachment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("filename", models.CharField(max_length=255)),
                (
                    "item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="test_app.orderitem",
                    ),
                ),
                (
                    "note",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="test_app.ordernote",
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
                "db_table": "test_order_note_attachment",
                "abstract": False,
                "constraints": [
                    django_rls_tenants.rls.constraints.RLSConstraint(
                        field="tenant", name="test_app_ordernoteattachment_rls_constraint"
                    )
                ],
            },
        ),
    ]
