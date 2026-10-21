"""M2M RLS policies of ``Label.tags`` and ``Board.labels``, written by ``check_rls --generate`` (#159)."""

from __future__ import annotations

import django_rls_tenants.operations
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0013_label_board"),
    ]

    operations = [
        django_rls_tenants.operations.AddM2MRLSPolicy(
            m2m_table="test_label_tags",
            from_model="test_app.Label",
            to_model="test_app.Tag",
            from_fk="label_id",
            to_fk="tag_id",
            to_tenant_fk=None,
        ),
        django_rls_tenants.operations.AddM2MRLSPolicy(
            m2m_table="test_board_labels",
            from_model="test_app.Board",
            to_model="test_app.Label",
            from_fk="board_id",
            to_fk="label_id",
        ),
    ]
