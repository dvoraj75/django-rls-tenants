"""Add ``SharedNote`` and ``NoteShare``, whose policies share rows across tenants (#59).

``SharedNote``'s constraint is in its ``CreateModel`` (the shape Django's optimizer
gives a new model), so its ``SharedWith`` predicate is built when the deferred SQL
runs at the end of the migration, after ``NoteShare`` is created. ``NoteShare``'s
constraint is an ``AddConstraint``, whose SQL runs at once.
"""

from __future__ import annotations

import django.db.models.deletion
import django_rls_tenants.rls.constraints
import django_rls_tenants.rls.predicates
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0010_loan_book_rls_policy"),
    ]

    operations = [
        migrations.CreateModel(
            name="SharedNote",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("title", models.CharField(max_length=100)),
                (
                    "partner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="partner_notes",
                        to="test_app.tenant",
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
                "db_table": "test_shared_note",
                "abstract": False,
                "constraints": [
                    django_rls_tenants.rls.constraints.RLSConstraint(
                        extra_predicates=[
                            django_rls_tenants.rls.predicates.SharedWith(
                                fk="note", tenant_fk="shared_with", through="test_app.NoteShare"
                            ),
                            django_rls_tenants.rls.predicates.TenantField("partner"),
                        ],
                        field="tenant",
                        name="test_app_sharednote_rls_constraint",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="NoteShare",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "shared_with",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="received_shares",
                        to="test_app.tenant",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="test_app.tenant"
                    ),
                ),
                (
                    "note",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shares",
                        to="test_app.sharednote",
                    ),
                ),
            ],
            options={
                "db_table": "test_note_share",
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="noteshare",
            constraint=django_rls_tenants.rls.constraints.RLSConstraint(
                extra_predicates=[django_rls_tenants.rls.predicates.TenantField("shared_with")],
                field="tenant",
                name="test_app_noteshare_rls_constraint",
            ),
        ),
    ]
