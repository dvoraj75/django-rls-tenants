"""Django management commands package for django-rls-tenants.

Provides CLI tools for verifying and applying Row-Level Security (RLS)
policies on protected tables:

- ``check_rls``: Verify that RLS policies are enabled and correctly
  configured on all ``RLSProtectedModel`` subclasses and M2M through tables.
- ``setup_m2m_rls``: Apply RLS policies retroactively to M2M through
  tables on existing deployments without re-running migrations.
"""
