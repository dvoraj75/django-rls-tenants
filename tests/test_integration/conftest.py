"""Integration test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _enforce_rls_for_integration(enforce_rls):
    """Auto-apply enforce_rls for all integration tests."""
