# Apply M2M RLS policies to auto-generated through tables.

import django_rls_tenants.operations
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("test_app", "0004_tag_selfrefmodel_project"),
    ]

    operations = [
        # Project.members: both sides RLS-protected (Project <-> ProtectedUser)
        django_rls_tenants.operations.AddM2MRLSPolicy(
            m2m_table="test_project_members",
            from_model="test_app.Project",
            to_model="test_app.ProtectedUser",
            from_fk="project_id",
            to_fk="protecteduser_id",
            from_tenant_fk="tenant",
            to_tenant_fk="tenant",
        ),
        # Project.tags: only Project side is RLS-protected (Project <-> Tag)
        django_rls_tenants.operations.AddM2MRLSPolicy(
            m2m_table="test_project_tags",
            from_model="test_app.Project",
            to_model="test_app.Tag",
            from_fk="project_id",
            to_fk="tag_id",
            from_tenant_fk="tenant",
            to_tenant_fk=None,
        ),
        # SelfRefModel.friends: self-referential, both FKs check same table
        django_rls_tenants.operations.AddM2MRLSPolicy(
            m2m_table="test_selfref_friends",
            from_model="test_app.SelfRefModel",
            to_model="test_app.SelfRefModel",
            from_fk="from_selfrefmodel_id",
            to_fk="to_selfrefmodel_id",
            from_tenant_fk="tenant",
            to_tenant_fk="tenant",
        ),
    ]
