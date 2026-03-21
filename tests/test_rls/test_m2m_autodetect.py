"""Tests for M2M auto-detection of RLSM2MConstraint.

Verifies that register_m2m_rls() correctly discovers M2M fields on
RLSProtectedModel subclasses and registers RLSM2MConstraint on
auto-generated through tables.
"""

from __future__ import annotations

from django_rls_tenants.rls.constraints import RLSM2MConstraint
from tests.test_app.models import Project, SelfRefModel


class TestAutoDetection:
    """Tests for register_m2m_rls() auto-detection."""

    def test_project_members_has_m2m_constraint(self):
        """Project.members through table has an RLSM2MConstraint."""
        through = Project.members.through
        constraints = [c for c in through._meta.constraints if isinstance(c, RLSM2MConstraint)]
        assert len(constraints) == 1

    def test_project_members_constraint_params(self):
        """Project.members constraint has correct from/to models."""
        through = Project.members.through
        constraint = next(c for c in through._meta.constraints if isinstance(c, RLSM2MConstraint))
        assert constraint.from_model == "test_app.Project"
        assert constraint.to_model == "test_app.ProtectedUser"
        assert constraint.from_tenant_fk == "tenant"
        assert constraint.to_tenant_fk == "tenant"

    def test_project_tags_has_m2m_constraint(self):
        """Project.tags through table has an RLSM2MConstraint."""
        through = Project.tags.through
        constraints = [c for c in through._meta.constraints if isinstance(c, RLSM2MConstraint)]
        assert len(constraints) == 1

    def test_project_tags_only_from_side_protected(self):
        """Project.tags: only Project side is RLS-protected, Tag is not."""
        through = Project.tags.through
        constraint = next(c for c in through._meta.constraints if isinstance(c, RLSM2MConstraint))
        assert constraint.from_tenant_fk == "tenant"
        assert constraint.to_tenant_fk is None

    def test_selfref_friends_has_m2m_constraint(self):
        """SelfRefModel.friends through table has an RLSM2MConstraint."""
        through = SelfRefModel.friends.through
        constraints = [c for c in through._meta.constraints if isinstance(c, RLSM2MConstraint)]
        assert len(constraints) == 1

    def test_selfref_both_sides_protected(self):
        """Self-referential M2M: both sides reference the same RLS-protected model."""
        through = SelfRefModel.friends.through
        constraint = next(c for c in through._meta.constraints if isinstance(c, RLSM2MConstraint))
        assert constraint.from_model == "test_app.SelfRefModel"
        assert constraint.to_model == "test_app.SelfRefModel"
        assert constraint.from_tenant_fk == "tenant"
        assert constraint.to_tenant_fk == "tenant"

    def test_no_duplicate_constraints_on_through_table(self):
        """Each through table has at most one RLSM2MConstraint."""
        for through_model in [
            Project.members.through,
            Project.tags.through,
            SelfRefModel.friends.through,
        ]:
            m2m_constraints = [
                c for c in through_model._meta.constraints if isinstance(c, RLSM2MConstraint)
            ]
            assert len(m2m_constraints) <= 1, (
                f"{through_model._meta.db_table} has {len(m2m_constraints)} "
                f"RLSM2MConstraint(s), expected at most 1"
            )
