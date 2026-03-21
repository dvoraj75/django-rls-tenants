"""Tests for AddM2MRLSPolicy migration operation."""

from __future__ import annotations

from unittest.mock import MagicMock

from django_rls_tenants.operations import AddM2MRLSPolicy


def _make_mock_apps(*table_map):
    """Create a mock apps object that resolves model paths to db_tables."""
    mapping = dict(table_map)
    apps = MagicMock()

    def get_model(app_label, model_name):
        path = f"{app_label}.{model_name}"
        model = MagicMock()
        model._meta.db_table = mapping.get(path, path.lower().replace(".", "_"))
        return model

    apps.get_model = get_model
    return apps


class TestAddM2MRLSPolicyForwards:
    """Tests for database_forwards."""

    def test_executes_create_policy_sql(self):
        op = AddM2MRLSPolicy(
            m2m_table="myapp_project_users",
            from_model="myapp.Project",
            to_model="myapp.User",
            from_fk="project_id",
            to_fk="user_id",
        )
        schema_editor = MagicMock()
        to_state = MagicMock()
        to_state.apps = _make_mock_apps(
            ("myapp.Project", "myapp_project"),
            ("myapp.User", "myapp_user"),
        )
        op.database_forwards("myapp", schema_editor, MagicMock(), to_state)
        assert schema_editor.execute.call_count == 1
        sql = schema_editor.execute.call_args[0][0]
        assert "CREATE POLICY" in sql
        assert "myapp_project_users_m2m_rls_policy" in sql
        assert "ENABLE ROW LEVEL SECURITY" in sql

    def test_both_sides_subqueries(self):
        op = AddM2MRLSPolicy(
            m2m_table="myapp_project_users",
            from_model="myapp.Project",
            to_model="myapp.User",
            from_fk="project_id",
            to_fk="user_id",
        )
        schema_editor = MagicMock()
        to_state = MagicMock()
        to_state.apps = _make_mock_apps(
            ("myapp.Project", "myapp_project"),
            ("myapp.User", "myapp_user"),
        )
        op.database_forwards("myapp", schema_editor, MagicMock(), to_state)
        sql = schema_editor.execute.call_args[0][0]
        assert "WHERE id = project_id AND tenant_id" in sql
        assert "WHERE id = user_id AND tenant_id" in sql

    def test_one_side_only(self):
        op = AddM2MRLSPolicy(
            m2m_table="myapp_project_tags",
            from_model="myapp.Project",
            to_model="myapp.Tag",
            from_fk="project_id",
            to_fk="tag_id",
            to_tenant_fk=None,
        )
        schema_editor = MagicMock()
        to_state = MagicMock()
        to_state.apps = _make_mock_apps(
            ("myapp.Project", "myapp_project"),
            ("myapp.Tag", "myapp_tag"),
        )
        op.database_forwards("myapp", schema_editor, MagicMock(), to_state)
        sql = schema_editor.execute.call_args[0][0]
        assert "WHERE id = project_id AND tenant_id" in sql
        assert "WHERE id = tag_id" not in sql


class TestAddM2MRLSPolicyBackwards:
    """Tests for database_backwards."""

    def test_drops_policy(self):
        op = AddM2MRLSPolicy(
            m2m_table="myapp_project_users",
            from_model="myapp.Project",
            to_model="myapp.User",
            from_fk="project_id",
            to_fk="user_id",
        )
        schema_editor = MagicMock()
        op.database_backwards("myapp", schema_editor, MagicMock(), MagicMock())
        sql = schema_editor.execute.call_args[0][0]
        assert "DROP POLICY IF EXISTS" in sql
        assert "DISABLE ROW LEVEL SECURITY" in sql


class TestAddM2MRLSPolicyDescribe:
    """Tests for describe()."""

    def test_describe(self):
        op = AddM2MRLSPolicy(
            m2m_table="myapp_project_users",
            from_model="myapp.Project",
            to_model="myapp.User",
            from_fk="project_id",
            to_fk="user_id",
        )
        assert "myapp_project_users" in op.describe()


class TestAddM2MRLSPolicyDeconstruct:
    """Tests for deconstruct()."""

    def test_roundtrip(self):
        op = AddM2MRLSPolicy(
            m2m_table="t",
            from_model="a.B",
            to_model="c.D",
            from_fk="b_id",
            to_fk="d_id",
            from_tenant_fk="org",
            to_tenant_fk=None,
            guc_tenant_var="custom.tenant",
            tenant_pk_type="uuid",
        )
        path, args, kwargs = op.deconstruct()
        assert "AddM2MRLSPolicy" in path
        assert args == []
        reconstructed = AddM2MRLSPolicy(**kwargs)
        assert reconstructed.m2m_table == op.m2m_table
        assert reconstructed.from_tenant_fk == "org"
        assert reconstructed.to_tenant_fk is None
        assert reconstructed.tenant_pk_type == "uuid"

    def test_defaults_omitted(self):
        op = AddM2MRLSPolicy(
            m2m_table="t",
            from_model="a.B",
            to_model="c.D",
            from_fk="b_id",
            to_fk="d_id",
        )
        _, _, kwargs = op.deconstruct()
        assert "from_tenant_fk" not in kwargs
        assert "to_tenant_fk" not in kwargs
        assert "guc_tenant_var" not in kwargs
        assert "guc_admin_var" not in kwargs
        assert "tenant_pk_type" not in kwargs


class TestAddM2MRLSPolicyStateForwards:
    """Tests for state_forwards (no-op)."""

    def test_state_forwards_is_noop(self):
        op = AddM2MRLSPolicy(
            m2m_table="t",
            from_model="a.B",
            to_model="c.D",
            from_fk="b_id",
            to_fk="d_id",
        )
        # Should not raise
        op.state_forwards("myapp", MagicMock())

    def test_reversible(self):
        op = AddM2MRLSPolicy(
            m2m_table="t",
            from_model="a.B",
            to_model="c.D",
            from_fk="b_id",
            to_fk="d_id",
        )
        assert op.reversible is True
